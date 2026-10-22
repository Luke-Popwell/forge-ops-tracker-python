"""Outbound HTTP span capture for the `requests` library (this SDK's own optional choice, not the
standard library urllib the client itself uses for delivery, since `requests` is what the Python
ecosystem overwhelmingly uses for outbound calls a host app makes on its own). Call once, at
startup:

    from forge_ops_tracker.integrations.requests import init_requests
    init_requests()

Not imported by forge_ops_tracker/__init__.py: requests is an optional dependency, so importing
this submodule specifically is what pulls it in, rather than every forge_ops_tracker user needing
requests installed.

Wraps requests.Session.send, the one method every call this library makes eventually goes through,
whether that's a bare requests.get(...) (which builds its own throwaway Session under the hood) or
a custom Session instance an app built and reused itself: the same "one hook point covers
everything built on top of it" reasoning gems/forge_ops_tracker's own Net::HTTP.prepend(Timing)
already relies on for Ruby's own HTTP ecosystem.

name is "<method> <host>", never the full URL: a request's own path or query string could carry an
id or a token, the same low-cardinality, no-secrets-in-a-label rule every other span name/
transaction_name in this SDK already follows.

Also propagates the current request's trace to the service being called, as a W3C `traceparent`
header (see trace_parent.py, and Configuration.propagate_traces/trace_propagation_targets for
turning it off or narrowing it to specific hosts). The header's parent id is this outbound call's
own span id, generated before the call is made (the span itself can only be recorded afterward,
once its duration is known) and then recorded with that exact id, so the downstream service's root
span points at a span that really exists in this trace. Only inside a request the Django/Flask
integrations are handling (a RequestState to take the trace id from): a Celery task or a script has
no trace to continue. Never added to a traceparent the app already set, nor to a request to
ForgeOps itself.
"""

import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from .. import _record_leaf_span, _request_state, _state
from ..trace_parent import HEADER, build, generate_span_id

_original_send = None


def init_requests():
    """Idempotent: calling this more than once (an app that inits it from more than one place)
    must never wrap requests.Session.send twice, which would otherwise double-record every single
    outbound call."""
    global _original_send
    if _original_send is not None:
        return
    _original_send = requests.Session.send

    def send(self, request, **kwargs):
        span_id = generate_span_id()
        request = _propagate_trace(request, span_id)
        started_at = datetime.now(timezone.utc)
        start = time.monotonic()
        response = None
        try:
            response = _original_send(self, request, **kwargs)
            return response
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            host = urlsplit(request.url).hostname or ""
            name = f"{request.method} {host}"
            # {} rather than a status key holding None: a request that raised before any response
            # came back (a timeout, a connection error) has no status at all to report, the same
            # "omit the key entirely" convention gems/forge_ops_tracker's own NetHTTP::Timing
            # already follows via Ruby's Hash#compact.
            data = {"status": response.status_code} if response is not None else {}
            _record_leaf_span(name, kind="http", started_at=started_at, duration_ms=duration_ms, data=data, span_id=span_id)

    requests.Session.send = send


def _propagate_trace(request, span_id):
    """Returns the request to actually send: a copy carrying the traceparent header when this call
    should get one, or the app's own request object unchanged otherwise. A copy, not the app's own
    PreparedRequest mutated in place: an app that sends the same prepared request twice (a retry)
    must get a fresh span id on the second attempt, not have it mistaken for a traceparent the app
    set itself. Never raises, since a failure to add a header must never stop the host app's own
    request from going out."""
    try:
        state = _request_state.get()
        if state is None:
            return request
        # requests' headers are a CaseInsensitiveDict, so this also finds "Traceparent".
        if request.headers is not None and HEADER in request.headers:
            return request
        configuration, _ = _state()
        if configuration.is_own_request(request.url):
            return request
        if not configuration.propagates_trace_to(urlsplit(request.url).hostname or ""):
            return request
        propagated = request.copy()
        propagated.headers[HEADER] = build(state.trace_id, span_id)
        return propagated
    except Exception:  # noqa: BLE001 (adding a header must never break the host app's own request)
        return request


def _reset_for_testing():
    """Not part of the public API: undoes init_requests()'s own monkeypatch between test cases,
    the same "module-level state reset between tests" shape forge_ops_tracker._reset_for_testing
    already has for this SDK's other global state."""
    global _original_send
    if _original_send is not None:
        requests.Session.send = _original_send
        _original_send = None
