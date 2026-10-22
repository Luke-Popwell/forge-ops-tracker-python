"""Django middleware. Add to MIDDLEWARE in settings.py:

    MIDDLEWARE = [
        ...,
        "django.contrib.auth.middleware.AuthenticationMiddleware",  # if not already there
        "forge_ops_tracker.integrations.django.ForgeOpsTrackerBreadcrumbContextMiddleware",
        "forge_ops_tracker.integrations.django.ForgeOpsTrackerTracingMiddleware",
        "forge_ops_tracker.integrations.django.ForgeOpsTrackerMiddleware",
        "forge_ops_tracker.integrations.django.ForgeOpsTrackerSessionTrackingMiddleware",
        "forge_ops_tracker.integrations.django.ForgeOpsTrackerPerformanceTrackingMiddleware",
        "forge_ops_tracker.integrations.django.ForgeOpsTrackerUserContextMiddleware",
    ]

ForgeOpsTrackerBreadcrumbContextMiddleware must be listed first (Django wraps middleware
outside-in, in list order, for the request phase): it has to already be wrapping everything below
it, both so a fresh trail exists by the time ForgeOpsTrackerPerformanceTrackingMiddleware's own
controller/query breadcrumbs try to record into it, and so ForgeOpsTrackerMiddleware's
process_exception can still read that same trail back (Django calls process_exception while still
nested inside every middleware listed above it, before any of their own __call__ methods return).

ForgeOpsTrackerTracingMiddleware must be listed before (outside) ForgeOpsTrackerPerformanceTrackingMiddleware
for the identical structural reason: its own captured-call-tree context has to already be active by
the time that middleware's connection.execute_wrapper starts recording query spans into it.

Every request also gets a trace id (continuing an incoming W3C traceparent header, or a fresh one),
published by whichever of ForgeOpsTrackerTracingMiddleware and ForgeOpsTrackerMiddleware runs
first, and the same one reused by the other (see _request_state_for below): an error reported
during the request carries it, plus the matched route as transaction_name/endpoint.

Not imported by forge_ops_tracker/__init__.py: Django is an optional
dependency, so importing this submodule specifically is what pulls it in,
rather than every forge_ops_tracker user needing Django installed.
"""

import time
from datetime import datetime, timezone

from django.db import connection

from .. import (
    _end_breadcrumb_trail,
    _end_request,
    _end_span_trace,
    _finish_span_trace,
    _new_request_state,
    _query_transaction_name,
    _record_breadcrumb,
    _record_database_query,
    _record_performance,
    _record_session,
    _request_state,
    _set_request_route,
    _start_breadcrumb_trail,
    _start_request,
    _start_span_trace,
    capture_exception,
    set_user,
)
from ..query_plans import STATEMENT_TIMEOUT, db_system_name

# An attribute on the HttpRequest itself, the Python equivalent of the Ruby gem's RequestState.for
# memoizing in the Rack env: both ForgeOpsTrackerTracingMiddleware and ForgeOpsTrackerMiddleware
# need this request's RequestState, in whichever order an app happened to list them, and must agree
# on one trace id. Whichever reaches the request first creates it; the other gets the same object.
_REQUEST_STATE_ATTR = "_forge_ops_tracker_request_state"
_UNSET = object()


def _request_state_for(request):
    state = getattr(request, _REQUEST_STATE_ATTR, _UNSET)
    if state is _UNSET:
        state = _new_request_state(request.META.get("HTTP_TRACEPARENT"))
        setattr(request, _REQUEST_STATE_ATTR, state)
    return state


def _note_route(request):
    """Names the current request after its matched URL pattern, once Django has resolved one: a
    no-op before resolution, for a request that never matched (a 404), or once already named.
    transaction_name keeps the "<method> <route>" shape the root span and performance samples
    already use (Django's route has no leading slash); endpoint adds one, "GET /users/<int:id>/"."""
    state = _request_state.get()
    resolver_match = getattr(request, "resolver_match", None)
    if state is None or state.transaction_name is not None or resolver_match is None:
        return
    route = resolver_match.route
    if route is None:
        return
    _set_request_route(request.method, route, endpoint_route="/" + route.lstrip("/"))


class ForgeOpsTrackerBreadcrumbContextMiddleware:
    """Gives every request a fresh, empty breadcrumb trail to accumulate into (via
    forge_ops_tracker.add_breadcrumb, or the automatic controller/query breadcrumbs
    ForgeOpsTrackerPerformanceTrackingMiddleware records below), so one request's trail never
    bleeds into another's, then lets ForgeOpsTrackerMiddleware.process_exception read it back off
    the same forge_ops_tracker._breadcrumbs context var when an error gets reported. A separate,
    independent middleware again, same "own concern, own class" shape every middleware in this
    module already follows: mirrors gems/forge_ops_tracker's own
    Middleware::BreadcrumbContext, which exists as its own Rack middleware for the identical
    reason (Rails.error.subscribe's own #report interface never receives the Rack env either).

    The finally-reset is load-bearing, not optional, the same reasoning
    ForgeOpsTrackerUserContextMiddleware's own set_user()-clearing finally already documents: a
    WSGI server reuses worker threads across requests, so leaving a trail set here would leak one
    request's breadcrumbs into a later, unrelated request handled on the same thread.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _start_breadcrumb_trail()
        try:
            return self.get_response(request)
        finally:
            _end_breadcrumb_trail(token)


class ForgeOpsTrackerTracingMiddleware:
    """Captures one slow request's own full nested call tree (this request's own controller/view
    span, plus every database/outbound-HTTP/manually-wrapped span nested under it) and delivers it
    as a single trace, only once its own total duration crosses
    configuration.trace_capture_threshold_ms: a normal, fast request never costs a single byte over
    the wire. A separate, independent middleware again, same "own concern, own class" shape every
    middleware in this module already follows: mirrors gems/forge_ops_tracker's own
    Middleware::SpanTracing, which gives every request a fresh SpanBuffer the same way.

    Unlike Rails (where ActiveSupport::Notifications' own process_action.action_controller fires
    only once the whole action, including everything nested inside it, has already run, separately
    from the Rack middleware that starts/ends the buffer), Django has no equivalent separate hook:
    the moment self.get_response(request) returns or raises IS the moment the root span's own
    duration becomes known, so recording it and deciding whether to deliver the trace both happen
    in this one middleware's own finally, via _finish_span_trace (see that function's own comment).

    Also publishes this request's RequestState (see _request_state_for) whether or not tracing is
    on, so the trace id exists for every request, and a request that arrived with a W3C
    traceparent header continues that trace: its root span's parent is the caller's span.

    The finally-reset is load-bearing, not optional, the same reasoning every other middleware in
    this module already documents for its own thread-local/context-var cleanup: a WSGI server
    reuses worker threads across requests, so leaving a trace's context set here would leak one
    request's spans into a later, unrelated request handled on the same thread.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_token = _start_request(_request_state_for(request))
        token = _start_span_trace()
        started_at = datetime.now(timezone.utc)
        start = time.monotonic()
        try:
            return self.get_response(request)
        except BaseException:
            state = _request_state.get()
            if state is not None:
                state.mark_errored()
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            resolver_match = getattr(request, "resolver_match", None)
            route = resolver_match.route if resolver_match else request.path
            _note_route(request)
            _finish_span_trace(f"{request.method} {route}", started_at, duration_ms)
            _end_span_trace(token)
            _end_request(request_token)


class ForgeOpsTrackerMiddleware:
    """Reports a view's unhandled exception. Also publishes this request's RequestState (a no-op
    when ForgeOpsTrackerTracingMiddleware, listed outside it, already did), and names it after the
    matched route in process_view, the earliest point Django knows the route: early enough that an
    error reported from inside the view itself (capture_exception in an except block) already
    carries transaction_name, endpoint, and trace_id."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _start_request(_request_state_for(request))
        try:
            return self.get_response(request)
        finally:
            _end_request(token)

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Returns None (implicitly), so Django goes on to call the view exactly as it would have.
        _note_route(request)

    def process_exception(self, request, exception):
        # Reports, then returns None: Django's own exception handling
        # (DEBUG page, a custom handler500, whatever's configured) behaves
        # exactly as if this middleware weren't installed. Only fires for
        # an exception that actually escaped the view uncaught; anything
        # your own code already catches never reaches here at all.
        # Django calls this while still nested inside every middleware's __call__, so the
        # request's state (trace id, route) is still published here, unlike Rails, where the
        # Ruby gem has to snapshot it onto the exception first.
        _note_route(request)
        capture_exception(exception, context=_request_context(request))


class ForgeOpsTrackerSessionTrackingMiddleware:
    """Counts every request as a session (crash-free unless an unhandled exception actually
    escaped it) toward the crash-free rate on a project's Releases page. A separate,
    independent middleware from ForgeOpsTrackerMiddleware above, not layered onto it, since
    session tracking runs regardless of whether error reporting is even configured: mirrors
    gems/forge_ops_tracker's own split between its Rack middleware (sessions) and its
    Rails.error hook (errors), two genuinely independent mechanisms.

    Unlike Rack (where an unhandled exception genuinely propagates back up through every
    middleware's own call), Django's own handler wraps every middleware layer in
    convert_exception_to_response, so by the time self.get_response(request) returns here, a
    view's unhandled exception has *already* been turned into a response (a 500, verified
    directly against a real request, not assumed): a plain try/except around get_response
    would never actually see it. status_code >= 500 is the real signal instead; the try/except
    stays too, purely defensively, for whatever this app's own code path might still manage to
    raise past that conversion (a broken middleware ahead of this one in the chain, say).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        crashed = False
        try:
            response = self.get_response(request)
            crashed = response.status_code >= 500
        except Exception:
            crashed = True
            raise
        finally:
            _record_session(crashed=crashed)
        return response


class ForgeOpsTrackerPerformanceTrackingMiddleware:
    """Times every request (so a dashboard widget on ForgeOps can show which parts of your app
    are actually slow, not just which ones raise), and every database query the request makes
    along the way. A separate, independent middleware again, same reasoning
    ForgeOpsTrackerSessionTrackingMiddleware's own comment documents.

    Unlike Express (which has no single call wrapping a whole request/response cycle), Django's
    own self.get_response(request) genuinely does wrap the rest of the chain, so timing is a
    plain wall-clock diff around that one call, no "finish" signal needed.

    transaction_name is "<HTTP method> <URL pattern>", e.g. "GET /users/<int:id>/", not the raw
    path: request.resolver_match.route is Django's own matched URL pattern, set once resolution
    succeeds (available here since get_response has already returned, resolution happens before
    the view runs, and it survives raising an exception from within the view itself); this keeps
    a distinct user id from exploding into its own separate transaction the way the literal path
    would. Falls back to request.path if nothing matched at all (a 404).

    Query timing wraps self.get_response(request) one layer deeper, in
    connection.execute_wrapper(self._record_query): Django's own documented instrumentation hook
    (unlike Rails' sql.active_record, there's no global "subscribe once, forever" mechanism here,
    only this scoped context manager), entered and exited fresh on every single request rather
    than installed once at import time, so a query run outside any request (a management command,
    a background thread) is never timed by this at all.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = None
        try:
            with connection.execute_wrapper(self._record_query):
                response = self.get_response(request)
                return response
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            resolver_match = getattr(request, "resolver_match", None)
            route = resolver_match.route if resolver_match else request.path
            transaction_name = f"{request.method} {route}"
            _record_performance(transaction_name, duration_ms, kind="controller")

            # Independent of track_performance above: an app could want the trail without the
            # timing data, or vice versa, and _record_breadcrumb already checks its own
            # track_breadcrumbs flag regardless, so there's no real cost to keeping the two
            # independent, the same reasoning gems/forge_ops_tracker/lib/forge_ops_tracker/
            # railtie.rb documents for the identical design choice.
            status = response.status_code if response is not None else None
            _record_breadcrumb(
                transaction_name,
                category="controller",
                level="error" if status is not None and status >= 500 else "info",
                data={k: v for k, v in {"status": status, "path": request.path}.items() if v is not None},
            )

    def _record_query(self, execute, sql, params, many, context):
        started_at = datetime.now(timezone.utc)
        start = time.monotonic()
        try:
            return execute(sql, params, many, context)
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            transaction_name = _query_transaction_name(sql)
            _record_performance(transaction_name, duration_ms, kind="query")
            _record_breadcrumb(transaction_name, category="query", data={"duration_ms": round(duration_ms, 1)})
            # A leaf span, parented onto whatever's currently open (this request's own root, or a
            # manually-wrapped forge_ops_tracker.span() block around it): a query has no children
            # of its own to nest anything under. A no-op with no trace currently being captured
            # (ForgeOpsTrackerTracingMiddleware not installed, or tracing off): recorded
            # regardless of whether this request eventually turns out to be slow, since that's
            # only decided once, at the very end, against the root span's own final duration.
            #
            # The span carries "db.statement" (masked) and "db.system" (the connection's vendor).
            # The raw SQL and params are only captured by the explain closure, for the opt-in
            # EXPLAIN (see query_plans.py), and held in memory until the planner's thread runs it.
            db_connection = context.get("connection") if isinstance(context, dict) else None
            db_system = _db_system(db_connection)
            statement = sql if isinstance(sql, str) else None
            explain = None
            if statement is not None and not many and db_system == "postgresql":
                alias = db_connection.alias

                def explain():
                    return _explain_on_new_connection(alias, statement, params)

            _record_database_query(transaction_name, started_at, duration_ms, statement, db_system=db_system, explain=explain)


def _db_system(db_connection):
    return db_system_name(getattr(db_connection, "vendor", None))


def _explain_on_new_connection(alias, sql, params):
    """Runs on the query planner's own thread, never the request's: opens a brand new connection
    for the same database alias (connections.create_connection, so it isn't even the worker
    thread's own connections[alias], and the request's connection and its transaction are never
    touched), makes its transaction READ ONLY with a statement_timeout, runs EXPLAIN (FORMAT JSON)
    with the original SQL and params, rolls back, and closes the connection."""
    from django.db import connections

    db_connection = connections.create_connection(alias)
    try:
        db_connection.set_autocommit(False)
        with db_connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}", params)
            row = cursor.fetchone()
        db_connection.rollback()
    finally:
        db_connection.close()
    return row[0] if row else None


class ForgeOpsTrackerUserContextMiddleware:
    """Automatically identifies the affected user for every error reported during a request,
    when `request.user` is present and authenticated: the same "observe, don't intercept" shape
    every middleware in this module already follows. `request.user` only exists once Django's own
    `django.contrib.auth.middleware.AuthenticationMiddleware` (or an equivalent third-party
    middleware setting the same attribute, e.g. DRF's own auth classes) has already run, so this
    middleware must be listed *after* it in MIDDLEWARE; this is a no-op (falls through to
    `getattr(request, "user", None)` being None) for an app with no such middleware installed at
    all, the same way gems/forge_ops_tracker's own Warden integration no-ops when
    `env["warden"]` was never mounted.

    Duck-types rather than assuming a specific User model: `pk` for id (Django's own primary-key
    attribute, present on every model instance regardless of what the app's own `AUTH_USER_MODEL`
    looks like), `email` if the attribute exists and is truthy, and `get_username()` (Django's own
    accessor, correct even when a custom `USERNAME_FIELD` isn't literally called "username") if
    present and truthy. A custom user object missing some of these just gets whatever subset
    applies, the same "duck-type, don't require a specific shape" approach every other
    framework-level auto-detection in this repo takes for its own language's dominant auth
    convention.

    Sets set_user() (see forge_ops_tracker/__init__.py's own doc) at the start of the request, so
    it composes with, rather than replaces, that manual API: an app's own view can still call
    set_user() itself afterward to override what was auto-detected for that one request. Always
    clears it again in a finally, the same reason SessionTrackingMiddleware's own crashed-flag
    logic is careful about cleanup: a WSGI server reuses worker threads across requests, so leaving
    a thread-local set here would leak one request's user into a later, unrelated request on the
    same thread.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            set_user(**_serialize_user(user))
        try:
            return self.get_response(request)
        finally:
            set_user()


def _serialize_user(user):
    fields = {}
    pk = getattr(user, "pk", None)
    if pk is not None:
        fields["id"] = str(pk)
    email = getattr(user, "email", None)
    if email:
        fields["email"] = email
    get_username = getattr(user, "get_username", None)
    username = get_username() if callable(get_username) else None
    if username:
        fields["username"] = username
    return fields


def _request_context(request):
    return {
        "path": request.path,
        "method": request.method,
        "query_string": request.META.get("QUERY_STRING", ""),
    }
