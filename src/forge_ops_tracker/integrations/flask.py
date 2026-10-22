"""Flask integration. Call once after creating the app:

    from forge_ops_tracker.integrations.flask import init_flask
    app = Flask(__name__)
    init_flask(app)

Not imported by forge_ops_tracker/__init__.py: Flask is an optional
dependency, so importing this submodule specifically is what pulls it
in, rather than every forge_ops_tracker user needing Flask installed.
"""

import time
from datetime import datetime, timezone

from flask import g, request
from flask.signals import got_request_exception

from .. import (
    _end_breadcrumb_trail,
    _end_request,
    _end_span_trace,
    _finish_span_trace,
    _new_request_state,
    _record_breadcrumb,
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


def init_flask(app):
    # Gives every request its RequestState (see request_state.py): a trace id, continuing an
    # incoming W3C traceparent header or a fresh one, whether or not tracing is on, since it's also
    # what links an error here to errors in other services. Named after the matched route right
    # away: Flask matches the URL before any before_request callback runs, so an error reported
    # from inside the view already carries transaction_name, endpoint, and trace_id.
    #
    # Registered first of all, ahead of even the breadcrumb trail below, for the same ordering
    # reasons that one gives: its before_request runs first (so _start_tracing can read the trace
    # id off it), and its teardown_request runs last (so _finish_tracing can still read the
    # errored flag). got_request_exception fires before any teardown_request, so an unhandled
    # exception is reported while this is still set.
    def _start_request_state():
        state = _new_request_state(request.headers.get("traceparent"))
        g._forge_ops_tracker_request_token = _start_request(state)
        if state is not None and request.url_rule is not None:
            _set_request_route(request.method, request.url_rule.rule)

    def _end_request_state(exc):
        _end_request(getattr(g, "_forge_ops_tracker_request_token", None))

    app.before_request(_start_request_state)
    app.teardown_request(_end_request_state)

    # Gives every request a fresh, empty breadcrumb trail (via forge_ops_tracker.add_breadcrumb,
    # or the automatic controller breadcrumb _record_duration below records), the same "own
    # concern, own hook" shape every other automatic behavior in this function already follows;
    # mirrors gems/forge_ops_tracker's own Middleware::BreadcrumbContext / Django's
    # ForgeOpsTrackerBreadcrumbContextMiddleware above it in this codebase.
    #
    # Registered first, deliberately, among this function's own before_request/teardown_request
    # calls: before_request callbacks run in registration order, so this has to run before
    # anything else that might read/record a breadcrumb (the view itself, _detect_user, and
    # eventually _record_duration below); teardown_request callbacks run in *reverse*
    # registration order, so registering _end_breadcrumbs here, first, is what makes it run
    # *last*, after _record_duration/_teardown/_clear_user's own teardown callbacks (registered
    # later, below) have already had a chance to record into or read the still-active trail.
    def _start_breadcrumbs():
        g._forge_ops_tracker_breadcrumb_token = _start_breadcrumb_trail()

    def _end_breadcrumbs(exc):
        token = getattr(g, "_forge_ops_tracker_breadcrumb_token", None)
        _end_breadcrumb_trail(token)

    app.before_request(_start_breadcrumbs)
    app.teardown_request(_end_breadcrumbs)

    # Captures one slow request's own full nested call tree (this request's own controller/view
    # span, plus every database/outbound-HTTP/manually-wrapped span nested under it) and delivers
    # it as a single trace, only once its own total duration crosses
    # configuration.trace_capture_threshold_ms: a normal, fast request never costs a single byte
    # over the wire. before_request/teardown_request, same reasoning session tracking/performance
    # timing below already use: teardown_request is the one hook Flask guarantees always runs,
    # exception or not. Unlike Django (which fires a separate process_action.action_controller-
    # style notification once the whole action's own duration is known), Flask has no equivalent
    # here either: the moment the view returns or raises IS the moment this request's own root
    # span duration becomes known, so recording it and deciding whether to deliver the trace both
    # happen together in _finish_span_trace (see that function's own comment), called from
    # _finish_tracing below.
    def _start_tracing():
        g._forge_ops_tracker_span_token = _start_span_trace()
        g._forge_ops_tracker_span_started_at = datetime.now(timezone.utc)
        g._forge_ops_tracker_span_start = time.monotonic()

    def _finish_tracing(exc):
        state = _request_state.get()
        if exc is not None and state is not None:
            state.mark_errored()
        token = getattr(g, "_forge_ops_tracker_span_token", None)
        start = getattr(g, "_forge_ops_tracker_span_start", None)
        if start is not None:
            duration_ms = (time.monotonic() - start) * 1000
            route = request.url_rule.rule if request.url_rule else request.path
            _finish_span_trace(f"{request.method} {route}", g._forge_ops_tracker_span_started_at, duration_ms)
        _end_span_trace(token)

    app.before_request(_start_tracing)
    app.teardown_request(_finish_tracing)

    def _handle(sender, exception, **extra):
        # Fires for a view's unhandled exception regardless of whether
        # Flask's own error handling ultimately produces a response:
        # reporting here doesn't change how Flask handles it afterward.
        # Only fires for an exception that actually escaped the view
        # uncaught; anything your own code already catches never reaches
        # here at all. Flask sends this signal before teardown_request runs, so the trail
        # _start_breadcrumbs above set up is still active when capture_exception reads it here.
        capture_exception(exception, context=_request_context())

    got_request_exception.connect(_handle, app)
    # Blinker (Flask's signal library) uses weak references by default:
    # keep a strong reference on the app itself so this handler isn't
    # garbage collected right after init_flask returns.
    app._forge_ops_tracker_signal_handler = _handle

    # Counts every request as a session (crash-free unless an unhandled exception actually
    # escaped it); teardown_request, not got_request_exception above: that signal only
    # fires on the exception path, and this needs to count every request, not just the
    # crashed ones. Flask guarantees teardown_request runs after every request, passing the
    # exception (or None) regardless of whether one occurred.
    def _teardown(exc):
        _record_session(crashed=exc is not None)

    app.teardown_request(_teardown)

    # Times every request: before_request/teardown_request rather than the more common
    # before_request/after_request pair, since after_request is skipped when a view raises and
    # no error handler produces a response (Flask's own docs are explicit about this);
    # teardown_request is the one hook Flask guarantees always runs, exception or not, same
    # reason it's what session tracking above already uses.
    def _start_timer():
        g._forge_ops_tracker_performance_start = time.monotonic()

    def _record_duration(exc):
        start = getattr(g, "_forge_ops_tracker_performance_start", None)
        if start is None:
            return
        duration_ms = (time.monotonic() - start) * 1000
        # request.url_rule is Django's request.resolver_match.route equivalent: the matched
        # route pattern (e.g. "/users/<int:id>"), not the raw path, set once routing succeeds
        # and None for a request that never matched at all (a 404).
        route = request.url_rule.rule if request.url_rule else request.path
        transaction_name = f"{request.method} {route}"
        _record_performance(transaction_name, duration_ms, kind="controller")

        # Independent of track_performance above, same reasoning
        # ForgeOpsTrackerPerformanceTrackingMiddleware's own Django equivalent documents.
        # teardown_request only ever hands back exc (the exception, if any), never the response
        # object Django's own middleware has access to here, so "did this request error" comes
        # from exc being not None rather than a status code.
        _record_breadcrumb(
            transaction_name,
            category="controller",
            level="error" if exc is not None else "info",
            data={"path": request.path},
        )

    app.before_request(_start_timer)
    app.teardown_request(_record_duration)

    # Auto-identifies the affected user for every error reported during a request, when
    # Flask-Login is installed and a user is actually logged in. Unlike Django (which ships its
    # own auth app that this integration can just assume) or Rails (Devise/Warden), Flask itself
    # has no built-in auth at all: Flask-Login is the de facto standard third-party choice, but a
    # genuinely optional one this package must not require, so the import happens lazily here,
    # inside the request hook, guarded by try/except, rather than at module load time.
    #
    # Flask-Login's own UserMixin only ever guarantees get_id(), is_active, is_anonymous, and
    # is_authenticated (verified directly against a real installed flask_login before relying on
    # this): there's no guaranteed email/username the way Django's get_username() is. id comes
    # from get_id() (Flask-Login's own accessor, always a str); email/username are read as plain
    # optional attributes, the common (but not Flask-Login-guaranteed) convention most apps follow
    # for their own User model, present only if the app actually defined them.
    #
    # Wrapped broadly (not just ImportError): confirmed directly that accessing current_user on an
    # app that has flask_login importable but never actually configured a LoginManager raises
    # AttributeError, not a clean "no user" signal, so this has to tolerate that failure mode too
    # rather than only guarding the import itself. Auto-detection breaking a request is a strictly
    # worse outcome than it silently not firing.
    def _detect_user():
        try:
            from flask_login import current_user
        except ImportError:
            return
        try:
            if not current_user.is_authenticated:
                return
            fields = {"id": current_user.get_id()}
            email = getattr(current_user, "email", None)
            if email:
                fields["email"] = email
            username = getattr(current_user, "username", None)
            if username:
                fields["username"] = username
            set_user(**fields)
        except Exception:  # noqa: BLE001, S110 (a broken user loader must never break the request)
            pass

    def _clear_user(exc):
        set_user()

    app.before_request(_detect_user)
    app.teardown_request(_clear_user)


def _request_context():
    return {
        "path": request.path,
        "method": request.method,
        "query_string": request.query_string.decode("utf-8", "replace"),
    }
