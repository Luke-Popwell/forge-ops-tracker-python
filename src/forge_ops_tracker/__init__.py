"""ForgeOps error tracking client.

    import forge_ops_tracker
    forge_ops_tracker.init(dsn="https://<api_key>@getforgeops.net/api/v1/events")

See the README for Django/Flask integration and what gets captured
automatically vs. what needs an explicit capture_exception() call.
"""

import contextvars
import functools
import inspect
import re
import sys
import threading
import time
from datetime import datetime, timezone

from . import changes as _changes
from . import sql_statement as _sql_statement
from . import strict_privacy as _strict_privacy
from .breadcrumb_buffer import BreadcrumbBuffer
from .client import Client
from .configuration import Configuration
from .delivery_queue import DeliveryQueue
from .event_builder import EventBuilder
from .metric_buffer import MetricBuffer
from .performance_flusher import PerformanceFlusher
from .query_plans import QueryPlanner, db_system_name
from .reporter import Reporter
from .request_state import RequestState
from .session_flusher import SessionFlusher
from .span_buffer import SpanBuffer, generate_span_id
from .span_queue import SpanQueue

__all__ = [
    "Configuration",
    "add_breadcrumb",
    "capture_exception",
    "capture_infrastructure_metric",
    "capture_metric",
    "flush_metrics",
    "init",
    "record_change",
    "set_user",
    "span",
]

_configuration = None
_reporter = None
_session_flusher = None
_performance_flusher = None
_metric_buffer = None
_infrastructure_metric_buffer = None
_span_queue = None
_change_queue = None
_query_planner = None
_change_snapshot_started = False
_lock = threading.Lock()
_original_excepthook = None
_environment_warning_logged = False
# Request-scoped, not global: a WSGI server (Django/Flask's own deployment model, what the
# integrations in this SDK actually target) runs one request per thread, so thread-local storage
# is the right isolation boundary, the same reasoning gems/forge_ops_tracker's own
# Thread.current[:forge_ops_tracker_current_user] already documents. Not correct under an ASGI/
# asyncio deployment, where several requests can share one OS thread; this SDK has no async
# integration today for that gap to matter yet.
_local = threading.local()
# Unlike _local above, this one genuinely does need to be a contextvars.ContextVar rather than
# threading.local: the breadcrumb trail also has to stay correctly scoped under Celery, where a
# worker's thread pool (not just its prefork processes) reuses one OS thread across many unrelated
# task runs, and under any future asyncio/ASGI integration, where several logical requests can
# interleave on a single OS thread as separate asyncio Tasks; threading.local would silently bleed
# one request's/task's trail into another's in either case. See breadcrumb_buffer.py's own module
# doc for the full reasoning. default=None (not a lazily-shared BreadcrumbBuffer instance) so
# add_breadcrumb below can tell "nothing started a trail yet" apart from "a trail exists but is
# empty" and create its own standalone one on demand, the same shape _local.user's own getattr
# default already has.
_breadcrumbs = contextvars.ContextVar("forge_ops_tracker_breadcrumbs", default=None)
# Same contextvars.ContextVar reasoning as _breadcrumbs above, for the identical reason: a
# request/task's own captured call tree has to stay isolated the same way its breadcrumb trail
# does, under Celery's thread-pool workers and any future asyncio/ASGI integration alike. default
# None so every leaf-span call site (the SQLAlchemy/Django query hooks, the outbound-HTTP hook,
# and the manual span() below) can tell "no trace is currently being captured" apart from "a trace
# exists but happens to have no spans yet" and just record nothing, the same "outside a request,
# do nothing" contract this whole feature promises.
_spans = contextvars.ContextVar("forge_ops_tracker_spans", default=None)
# The current request's RequestState (trace id, remote parent span id, transaction name, endpoint,
# errored flag; see request_state.py), published by the Django/Flask integrations for the duration
# of a request. A ContextVar for the same reasons as _breadcrumbs/_spans above. Not gated on
# track_tracing: the trace id links an error to errors in other services even when no spans are
# ever recorded. default None, meaning "not inside a request": capture_exception, the tracing
# hooks, and the outbound-HTTP integration all behave exactly as they did before this existed.
_request_state = contextvars.ContextVar("forge_ops_tracker_request_state", default=None)


def _state():
    global _configuration, _reporter
    if _reporter is None:
        with _lock:
            if _reporter is None:
                _configuration = Configuration()
                client = Client(_configuration)
                delivery_queue = DeliveryQueue(_configuration, client)
                _reporter = Reporter(_configuration, EventBuilder(_configuration), delivery_queue)
    return _configuration, _reporter


def _session_state():
    global _session_flusher
    configuration, _ = _state()
    if _session_flusher is None:
        with _lock:
            if _session_flusher is None:
                _session_flusher = SessionFlusher(configuration, Client(configuration))
    return configuration, _session_flusher


def _record_session(crashed):
    """Internal; called by the Django/Flask session-tracking integrations, never by host app
    code directly (there's nothing for a caller to decide here beyond what the middleware/
    hook itself already observed)."""
    configuration, flusher = _session_state()
    if not configuration.track_sessions or not configuration.is_enabled():
        return
    flusher.record_session(crashed=crashed)


def _performance_state():
    global _performance_flusher
    configuration, _ = _state()
    if _performance_flusher is None:
        with _lock:
            if _performance_flusher is None:
                _performance_flusher = PerformanceFlusher(configuration, Client(configuration))
    return configuration, _performance_flusher


def _record_performance(transaction_name, duration_ms, kind="controller"):
    """Internal; called by the Django/Flask/Celery/SQLAlchemy performance-tracking integrations,
    never by host app code directly, same reasoning as _record_session above. kind
    ("controller"/"job"/"query") distinguishes what was actually timed; see each integration's
    own call site for which kind it passes."""
    configuration, flusher = _performance_state()
    if not configuration.track_performance or not configuration.is_enabled():
        return
    flusher.record(transaction_name, duration_ms, kind=kind)


def _metric_buffers():
    """The two metric buffers, created together on first use: independent of each other (a script
    may only ever call one), but cheap enough that creating both is simpler than tracking which."""
    global _metric_buffer, _infrastructure_metric_buffer
    configuration, _ = _state()
    if _metric_buffer is None:
        with _lock:
            if _metric_buffer is None:
                client = Client(configuration)
                _metric_buffer = MetricBuffer(configuration, client.deliver_metrics, lambda: configuration.metric_flush_interval)
                _infrastructure_metric_buffer = MetricBuffer(
                    configuration, client.deliver_infrastructure_metrics, lambda: configuration.infrastructure_metric_flush_interval
                )
    return configuration, _metric_buffer, _infrastructure_metric_buffer


def capture_metric(name, value=1.0):
    """Records a named business metric (a signup, a payment, anything you want to name), buffered and
    flushed periodically as one batch rather than one network call per capture. `value` defaults to 1.0
    so a bare counter-style call needs no argument; pass one for a real magnitude (`capture_metric(
    "payment", 49.0)`); it may be negative (a refund). A no-op when the client isn't enabled (no DSN,
    or this environment isn't in `enabled_environments`), and a NaN or infinite value is dropped."""
    configuration, metrics, _ = _metric_buffers()
    if not configuration.is_enabled():
        return
    metrics.record({"metric_name": name, "value": value, "environment": configuration.environment, "release": configuration.release})


def capture_infrastructure_metric(name, value, hostname=None):
    """Records one infrastructure reading (CPU, memory, disk, anything else a script of yours
    reads) from one of your own hosts. `hostname` defaults to `Configuration.server_name`, so a script
    running on the box it reports about needs no argument. Same buffered-batch delivery and
    no-op-when-disabled contract as `capture_metric`. A short-lived cron job should call
    `flush_metrics()` before it exits if it can't rely on the exit hook (an `os._exit`, a kill)."""
    configuration, _, infrastructure = _metric_buffers()
    if not configuration.is_enabled():
        return
    infrastructure.record({"metric_name": name, "value": value, "hostname": hostname or configuration.server_name})


def flush_metrics():
    """Delivers every buffered metric and infrastructure reading right now, instead of waiting for the
    next flush interval."""
    _, metrics, infrastructure = _metric_buffers()
    metrics.flush()
    infrastructure.flush()


def _change_queue_state():
    global _change_queue
    configuration, _ = _state()
    if _change_queue is None:
        with _lock:
            if _change_queue is None:
                _change_queue = DeliveryQueue(configuration, Client(configuration), deliver_with="deliver_change")
    return configuration, _change_queue


def record_change(kind, title, details=None, environment=None, service=None, actor=None, url=None, id=None, occurred_at=None):
    """Records one thing that changed in a running system, so ForgeOps can show it next to the
    errors and slowdowns that followed:

        forge_ops_tracker.record_change("feature_flag", "Enabled new_checkout for 10%", details={"rollout": 10})

    `kind` is one of feature_flag, config, migration, dependency, infrastructure, or other (anything
    else is sent as "other"). `environment` defaults to the configured one; `occurred_at` (a datetime
    or an ISO 8601 string) defaults to now; `id` is an optional idempotency key. Queued and sent from
    a background thread like error events, so it never blocks the caller. A no-op when the client
    isn't enabled. Never raises: returns False when nothing was queued."""
    try:
        configuration, change_queue = _change_queue_state()
        if not configuration.is_enabled():
            return False
        payload = _changes.build_change(
            configuration, kind, title, details=details, environment=environment, service=service, actor=actor, url=url, id=id, occurred_at=occurred_at
        )
        if payload is None:
            return False
        return change_queue.push(payload)
    except Exception as e:  # noqa: BLE001 (this must never raise back into the host app)
        _state()[0].logger.debug("[forge_ops_tracker] record_change failed: %s: %s", type(e).__name__, e)
        return False


def _start_change_snapshot(configuration):
    """Internal: sends the one startup snapshot this process sends (see changes.py), from a daemon
    thread so it never delays startup. Called by init(); a second call is a no-op. Does nothing, and
    doesn't use up the once, when the client isn't enabled or detect_changes is off. Never raises."""
    global _change_snapshot_started
    try:
        if not (configuration.is_enabled() and configuration.detect_changes):
            return False
        with _lock:
            if _change_snapshot_started:
                return False
            _change_snapshot_started = True
        threading.Thread(target=_deliver_change_snapshot, args=(configuration,), daemon=True).start()
        return True
    except Exception:  # noqa: BLE001 (a snapshot must never fail startup)
        return False


def _deliver_change_snapshot(configuration):
    try:
        return Client(configuration).deliver_change_snapshot(_changes.build_snapshot(configuration))
    except Exception as e:  # noqa: BLE001 (runs on its own thread; nothing to raise to)
        configuration.logger.debug("[forge_ops_tracker] change snapshot failed: %s: %s", type(e).__name__, e)
        return False


def _span_queue_state():
    global _span_queue
    configuration, _ = _state()
    if _span_queue is None:
        with _lock:
            if _span_queue is None:
                _span_queue = SpanQueue(configuration, Client(configuration))
    return configuration, _span_queue


def _query_planner_state():
    global _query_planner
    configuration, _ = _state()
    if _query_planner is None:
        with _lock:
            if _query_planner is None:
                delivery_queue = DeliveryQueue(configuration, Client(configuration), deliver_with="deliver_query_plan")
                _query_planner = QueryPlanner(configuration, delivery_queue)
    return configuration, _query_planner


def _database_span_data(masked_statement, db_system, data=None):
    data = dict(data or {})
    if masked_statement is not None:
        data["db.statement"] = masked_statement
    db_system = db_system_name(db_system)
    if db_system:
        data["db.system"] = db_system
    return data


def _record_database_query(name, started_at, duration_ms, statement, db_system=None, explain=None):
    """Internal; called by the Django/SQLAlchemy query hooks once a query has finished. Records its
    database span (like _record_leaf_span below) carrying "db.statement", the statement with every
    string and number masked to "?", and "db.system", then offers the query to the opt-in EXPLAIN
    (see query_plans.py). explain, when given, is a no-argument callable that runs EXPLAIN on a
    separate connection; it's only ever called on the planner's own thread. Never raises."""
    masked = None
    try:
        buffer = _spans.get()
        if buffer is not None:
            masked = _sql_statement.mask_for_span(statement, db_system)
            buffer.record(
                span_id=generate_span_id(),
                name=name,
                kind="database",
                started_at=started_at,
                duration_ms=duration_ms,
                data=_database_span_data(masked, db_system),
            )
        if explain is not None:
            configuration, _ = _state()
            if configuration.explain_slow_queries:
                _query_planner_state()[1].consider(statement, db_system, duration_ms, explain, masked=masked)
    except Exception as e:  # noqa: BLE001 (runs inside the app's own query path)
        _state()[0].logger.debug("[forge_ops_tracker] database span failed: %s: %s", type(e).__name__, e)


def _new_request_state(traceparent):
    """Internal: called by the Django/Flask integrations once per request, with the incoming
    traceparent header's raw value (or None). Returns a RequestState continuing that trace, or a
    fresh one, or None when the client isn't enabled at all (the same "skip entirely" gating every
    other automatic source in this module applies)."""
    configuration, _ = _state()
    if not configuration.is_enabled():
        return None
    return RequestState.from_traceparent(traceparent)


def _start_request(state):
    """Internal: publishes state as the current request's for the rest of this context. Returns a
    contextvars.Token for _end_request below, or None (a no-op token) when state is None or already
    the current one: Django can reach this from two of its middlewares for the same request (see
    integrations/django.py), and only the outermost one should reset it again."""
    if state is None or _request_state.get() is state:
        return None
    return _request_state.set(state)


def _end_request(token):
    """Internal: pairs with _start_request above, always called from a finally so a request's state
    is cleared even when it raises, for the same "worker threads get reused across requests"
    reasoning _end_breadcrumb_trail's own doc gives."""
    if token is not None:
        _request_state.reset(token)


def _set_request_route(method, route, endpoint_route=None):
    """Internal: called by the Django/Flask integrations as soon as the framework has matched this
    request to a route, early enough that an error reported from inside the view already carries
    both. transaction_name is "<method> <route>", the same name the request's root span and
    performance samples use; endpoint is "<method> <endpoint_route or route>", the human-readable
    route pattern (never the raw path, so no ids or tokens). A no-op outside a request."""
    state = _request_state.get()
    if state is None or route is None:
        return
    state.transaction_name = f"{method} {route}"
    state.endpoint = f"{method} {endpoint_route or route}"


def _start_span_trace():
    """Internal: called by the Django/Flask tracing hooks at the start of a request, to give it a
    fresh SpanBuffer for the automatic database/outbound-HTTP spans below (and any manual span()
    call during it) to accumulate into. Returns None, a no-op token, when track_tracing is off or
    the client isn't enabled at all, the same "skip entirely" gating _record_performance/
    _record_session already apply to their own automatic sources.

    Returns a contextvars.Token rather than nothing, same reasoning _start_breadcrumb_trail's own
    doc gives: _end_span_trace below resets the context var to exactly whatever it held before
    this call, rather than always clearing it back to empty regardless of what else might have
    been there."""
    configuration, _ = _state()
    if not (configuration.track_tracing and configuration.is_enabled()):
        return None
    # The trace id and remote parent come from the request's RequestState when there is one (see
    # _start_request), so this trace and any error event from the same request agree on the trace.
    state = _request_state.get()
    if state is None:
        return _spans.set(SpanBuffer(configuration))
    return _spans.set(SpanBuffer(configuration, trace_id=state.trace_id, parent_span_id=state.parent_span_id))


def _finish_span_trace(name, started_at, duration_ms):
    """Internal: called by the Django/Flask tracing hooks once the whole request has finished
    (get_response/the view has returned or raised), to record its own root span and decide whether
    the resulting trace crossed configuration.trace_capture_threshold_ms and is worth sending at
    all. Unlike Rails (where a separate process_action.action_controller notification fires only
    once the whole action has already run, distinct from the Rack middleware that starts/ends the
    buffer), Django/Flask have no such separate hook: the moment the view returns IS the moment the
    root's own duration becomes known, so recording the root span and deciding whether to deliver
    it both happen here, in one place, rather than split across two call sites the way the Ruby gem
    splits them across Railtie and Middleware::SpanTracing.

    A no-op if no trace is currently being captured (track_tracing was off, or the client wasn't
    enabled, when this request started)."""
    buffer = _spans.get()
    if buffer is None:
        return

    configuration, _ = _state()
    buffer.record(span_id=buffer.root_span_id, name=name, kind="controller", started_at=started_at, duration_ms=duration_ms, root=True)

    # An errored request's trace is always sent too, however fast it was, since the waterfall of
    # what led up to an error is exactly what an issue page wants to show next to it. "Errored"
    # means capture_exception ran during the request (the integrations' own unhandled-exception
    # hooks call it too) or the view raised straight through the tracing hook.
    state = _request_state.get()
    errored = state is not None and state.errored
    if buffer.is_slow(configuration.trace_capture_threshold_ms) or (errored and buffer.root_recorded):
        spans = buffer.spans
        # Under strict privacy every span goes through strict_privacy.span: an outbound call named
        # by its method and host only, a root span named by a raw path (a 404 never matched a route)
        # cut down to its first segment, and no span data but the masked statement and db.system.
        if configuration.strict_privacy:
            spans = [_strict_privacy.span(entry) for entry in spans]
        _span_queue_state()[1].push(trace_id=buffer.trace_id, spans=spans)


def _end_span_trace(token):
    """Internal: pairs with _start_span_trace above, always called from a finally so a trace's own
    context is cleared even when the request raises. A no-op if _start_span_trace itself returned
    None. Same "WSGI worker threads get reused across requests" reasoning _end_breadcrumb_trail's
    own doc gives for why this can't just be skipped."""
    if token is not None:
        _spans.reset(token)


def _record_leaf_span(name, kind, started_at, duration_ms, data=None, span_id=None):
    """Internal; called by the SQLAlchemy/Django query hooks and the outbound-HTTP hook, never by
    host app code directly, same reasoning as _record_performance above. A leaf span, parented onto
    whatever is currently open (the request's own root, or a manually wrapped forge_ops_tracker.
    span() block around it): none of these have children of their own to nest anything under, so
    unlike the root span in _finish_span_trace above, this never touches the buffer's own stack,
    just records against whatever's already on top of it. A no-op with no trace currently being
    captured: this must never fabricate a trace with no request to belong to. span_id is only
    passed by the outbound-HTTP hook, which has to pick it before the call goes out so the
    traceparent header it sends names this exact span."""
    buffer = _spans.get()
    if buffer is None:
        return
    buffer.record(span_id=span_id or generate_span_id(), name=name, kind=kind, started_at=started_at, duration_ms=duration_ms, data=data)


def span(name, kind="service", data=None, statement=None, db_system=None):
    """Manually marks a logically distinct chunk of code (typically a service-layer call) as its
    own span in whatever trace is currently being captured for this request:

        with forge_ops_tracker.span("PaymentService.charge"):
            charge_card(order)

    Also works as a decorator, on a plain function or a coroutine function (where the span covers
    the awaited body, not just creating the coroutine), and as an async context manager:

        @forge_ops_tracker.span("PaymentService.charge")
        def charge(order):
            ...

        @forge_ops_tracker.span("PaymentService.refund")
        async def refund(order):
            ...

    There's no way to auto-detect "this is a logically distinct service layer" the way a SQL query
    already has a real instrumentation hook to extend, so unlike every other span kind in this
    module, this is the one manual, public piece of the tracing API a customer has to reach for by
    hand: mirrors gems/forge_ops_tracker's own ForgeOpsTracker.span(name, kind:) { ... }.

    A database span you time yourself (a query through a driver this SDK doesn't instrument) can
    carry its SQL: pass statement= (and optionally db_system=, e.g. "postgresql") with
    kind="database", and the span's data gets "db.statement", with every string and number
    replaced by "?", and "db.system". The unmasked statement is never sent:

        with forge_ops_tracker.span("Load orders", kind="database", statement=sql, db_system="postgresql"):
            cursor.execute(sql, params)

    The span is open only in the execution context that entered it (see span_buffer.py): asyncio
    tasks started inside the block, or threads run with a copy of its context, nest under it, but
    two blocks running at the same time in sibling tasks each stay the parent of only their own
    spans.

    A no-op that just runs the wrapped code, recording nothing, outside of any request currently
    being traced (no per-request buffer active, e.g. a plain script, or a request that started
    before tracing was enabled) or with track_tracing turned off entirely: this must never raise,
    and never fabricate a trace with no request to belong to."""
    return _Span(name, kind, data, statement, db_system)


class _Span:
    """What span() returns. A class rather than a contextlib.contextmanager, whose decorator form
    would close the span as soon as a coroutine function returned its coroutine, before any of the
    body had run. Each decorated call gets a fresh _Span, so one decorated function running in
    several tasks at once keeps a separate span per call."""

    def __init__(self, name, kind, data, statement, db_system):
        self._args = (name, kind, data, statement, db_system)
        self._open = None

    def __enter__(self):
        configuration, _ = _state()
        buffer = _spans.get()
        if buffer is None or not configuration.track_tracing:
            return
        span_id = generate_span_id()
        token = buffer.push(span_id)
        self._open = (buffer, span_id, token, datetime.now(timezone.utc), time.monotonic())

    def __exit__(self, *exc_info):
        if self._open is None:
            return False
        buffer, span_id, token, started_at, start = self._open
        self._open = None
        buffer.pop(token)
        duration_ms = (time.monotonic() - start) * 1000
        name, kind, data, statement, db_system = self._args
        if kind == "database" and (statement is not None or db_system):
            data = _database_span_data(_sql_statement.mask_for_span(statement, db_system), db_system, data)
        buffer.record(span_id=span_id, name=name, kind=kind, started_at=started_at, duration_ms=duration_ms, data=data)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc_info):
        return self.__exit__(*exc_info)

    def __call__(self, func):
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                with _Span(*self._args):
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with _Span(*self._args):
                return func(*args, **kwargs)

        return wrapper


# Neither Django's connection.execute_wrapper nor SQLAlchemy's before/after_cursor_execute events
# (see integrations/django.py and integrations/sqlalchemy.py) hand over a friendly auto-generated
# label the way Rails' own sql.active_record payload does; only the raw SQL string. Raw SQL is
# both too high-cardinality for a transaction_name (every slightly different query shape would
# become its own bucket) and a real, if usually parameterized, risk of leaking a literal value.
# Regexes out "<VERB> <table>" ("SELECT auth_user", "INSERT INTO orders") for the two shapes that
# actually carry a table name in a predictable place; falls back to just the first SQL keyword
# ("PRAGMA", "BEGIN") for anything else, or the literal string "SQL" if even that fails. Not a
# real SQL parser, deliberately: the same "close enough, low-cardinality" trade-off Rails' own
# SCHEMA/cached-query skip already accepts for the Ruby SDK.
_SELECT_DELETE_RE = re.compile(r"^\s*(SELECT|DELETE)\b.*?\bFROM\s+[\"'`]?(\w+)", re.IGNORECASE | re.DOTALL)
_INSERT_UPDATE_RE = re.compile(r"^\s*(INSERT INTO|UPDATE)\s+[\"'`]?(\w+)", re.IGNORECASE)


def _query_transaction_name(sql):
    match = _SELECT_DELETE_RE.match(sql) or _INSERT_UPDATE_RE.match(sql)
    if match:
        return f"{match.group(1).upper()} {match.group(2)}"

    words = sql.strip().split(None, 1)
    return words[0].upper() if words else "SQL"


def init(dsn=None, **overrides):
    """Configure the client. Call once at startup (Django settings.py, or
    right after creating a Flask app). Any Configuration attribute can be
    overridden by keyword, e.g. init(dsn=..., release=..., environment=...).
    """
    configuration, _ = _state()
    if dsn is not None:
        configuration.dsn = dsn
    for key, value in overrides.items():
        if not hasattr(configuration, key):
            raise TypeError(f"Configuration has no attribute {key!r}")
        setattr(configuration, key, value)

    _warn_if_environment_disabled(configuration)

    if configuration.install_excepthook:
        _install_excepthook()

    _start_change_snapshot(configuration)

    return configuration


def _warn_if_environment_disabled(configuration):
    """Logs one warning per process when a DSN is set but the resolved environment isn't enabled,
    so a host that would silently send nothing says so once at startup. Never logs without a
    DSN."""
    global _environment_warning_logged
    message = configuration.disabled_environment_warning()
    if message is None:
        return
    with _lock:
        if _environment_warning_logged:
            return
        _environment_warning_logged = True
    configuration.logger.warning(message)


def capture_exception(exc=None, context=None, user=None):
    """Report an exception you've already caught. `exc` defaults to
    whichever exception is currently being handled, so this can usually
    just be called as capture_exception() from inside an `except:` block.
    `user` defaults to whatever set_user() last set for this thread, if
    anything; pass one explicitly to override that for this one report.

    Inside a request the Django/Flask integrations are handling, the event
    also carries the request's transaction_name, endpoint, and trace_id,
    and the request's trace is sent however fast it was."""
    if exc is None:
        exc = sys.exc_info()[1]
    if exc is None:
        return

    _, reporter = _state()
    buffer = _breadcrumbs.get()
    state = _request_state.get()
    if state is not None:
        state.mark_errored()
    reporter.report(
        exc,
        context=context,
        user=user if user is not None else getattr(_local, "user", None),
        breadcrumbs=buffer.all() if buffer is not None else None,
        transaction_name=state.transaction_name if state is not None else None,
        endpoint=state.endpoint if state is not None else None,
        trace_id=state.trace_id if state is not None else None,
    )


def set_user(id=None, email=None, username=None):
    """Manually attaches an affected user to whatever gets reported for the rest of this thread
    (a WSGI request, a background worker, a console session): see capture_exception()'s own doc
    for why thread-local, not global. id/email/username are all independently optional; call with
    none of them to clear whatever was set, e.g. once a request finishes."""
    user = {k: v for k, v in {"id": id, "email": email, "username": username}.items() if v is not None}
    _local.user = user or None


def add_breadcrumb(message, category="custom", level="info", data=None):
    """Adds one breadcrumb to the current request/task's trail (or, outside one entirely, a
    console session's own trail for the rest of this context: see _start_breadcrumb_trail below
    for how a request/task gets its own buffer to start with). Lazily creates a buffer on this
    context if nothing started one yet, the same "works standalone, no specific setup required"
    shape set_user already has above. Works whether or not track_breadcrumbs is on: that flag only
    gates the automatic sources each integration records via _record_breadcrumb below, never this
    manual call, matching gems/forge_ops_tracker's own ForgeOpsTracker.add_breadcrumb."""
    buffer = _breadcrumbs.get()
    if buffer is None:
        configuration, _ = _state()
        buffer = BreadcrumbBuffer(configuration)
        _breadcrumbs.set(buffer)
    buffer.add(category=category, message=message, level=level, data=data)


def _start_breadcrumb_trail():
    """Internal: called by the Django/Flask/Celery breadcrumb-context hooks (never by host app
    code directly) at the start of a request or task, to give it a fresh, empty trail for the
    automatic controller/query/job breadcrumbs below (and any manual add_breadcrumb call during
    it) to accumulate into. Returns None, a no-op token, when track_breadcrumbs is off or the
    client isn't enabled at all, the same "skip entirely" gating _record_performance/_record_session
    already apply to their own automatic sources; add_breadcrumb above still works regardless of
    this by lazily creating its own standalone buffer.

    Returns a contextvars.Token rather than nothing: _end_breadcrumb_trail below resets the context
    var to exactly whatever it held before this call (ordinarily nothing, but correctly nested if
    this ever runs while a trail is already active, e.g. a Celery task dispatched synchronously
    from inside another task's own run), rather than always clearing it back to empty regardless of
    what else might have been there.
    """
    configuration, _ = _state()
    if not (configuration.track_breadcrumbs and configuration.is_enabled()):
        return None
    return _breadcrumbs.set(BreadcrumbBuffer(configuration))


def _end_breadcrumb_trail(token):
    """Internal: pairs with _start_breadcrumb_trail above, always called from a finally so a trail
    is cleared even when the request/task raises. A no-op if _start_breadcrumb_trail itself
    returned None (nothing was started). The same reasoning
    ForgeOpsTrackerUserContextMiddleware's own set_user()-clearing finally already documents
    applies here too: a WSGI worker thread (or a Celery worker's own thread pool) reuses one OS
    thread across many unrelated requests/tasks, so leaving a trail set would leak one into the
    next one handled on the same thread."""
    if token is not None:
        _breadcrumbs.reset(token)


def _record_breadcrumb(message, category, level="info", data=None):
    """Internal; called by the Django/Flask/SQLAlchemy/Celery breadcrumb-tracking integrations,
    never by host app code directly, same reasoning as _record_performance above. Independent of
    track_performance: an app could want the trail without the timing data, or vice versa, and
    each call site already has to check its own flag regardless, so there's no real cost to
    keeping the two independent rather than tying breadcrumbs to whether performance monitoring
    happens to be on (mirrors gems/forge_ops_tracker/lib/forge_ops_tracker/railtie.rb's own
    reasoning for the same design choice)."""
    configuration, _ = _state()
    if not configuration.track_breadcrumbs or not configuration.is_enabled():
        return
    add_breadcrumb(message, category=category, level=level, data=data)


def _install_excepthook():
    # Reports anything that crashes the whole interpreter (a plain
    # script, a management command, a worker's own top-level loop) with
    # no further wiring: the same "unhandled needs no wiring" case
    # Rails.error/ASP.NET Core's middleware cover automatically. This does
    # *not* catch a web request's unhandled exception under a WSGI
    # server: Django/Flask catch that themselves before it ever reaches
    # here, which is what the integrations in forge_ops_tracker.integrations
    # are for.
    global _original_excepthook
    if _original_excepthook is not None:
        return  # already installed

    _original_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        try:
            capture_exception(exc_value)
        finally:
            _original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook


def _reset_for_testing():
    """Not part of the public API: resets module-level state between
    test cases."""
    global _configuration, _reporter, _session_flusher, _performance_flusher, _span_queue, _original_excepthook
    global _metric_buffer, _infrastructure_metric_buffer, _change_queue, _change_snapshot_started, _query_planner
    global _environment_warning_logged
    _environment_warning_logged = False
    if _original_excepthook is not None:
        sys.excepthook = _original_excepthook
    _configuration = None
    _reporter = None
    _session_flusher = None
    _performance_flusher = None
    _metric_buffer = None
    _infrastructure_metric_buffer = None
    _span_queue = None
    _change_queue = None
    _query_planner = None
    _change_snapshot_started = False
    _original_excepthook = None
    _local.user = None
    _breadcrumbs.set(None)
    _spans.set(None)
    _request_state.set(None)
