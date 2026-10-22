"""Everything this SDK knows about the current request that isn't already owned by one of the
older, single-purpose context vars (the user, the breadcrumb trail, the span buffer): which trace it
belongs to, which remote span called it (if any), what it's called, and whether it errored. One
instance per request, published as forge_ops_tracker._request_state for the duration of the request
by the Django/Flask integrations, so capture_exception, the tracing hooks, and the outbound-HTTP
integration can all reach it. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/request_state.rb.

The trace_id exists for every request, not only when track_tracing is on: it's also what an error
event carries so ForgeOps can link it to errors reported by other services taking part in the same
trace, which works on any plan and with tracing off entirely.
"""

from . import trace_parent


class RequestState:
    def __init__(self, trace_id=None, parent_span_id=None):
        self.trace_id = trace_id or trace_parent.generate_trace_id()
        self.parent_span_id = parent_span_id
        # Both set by the Django/Flask integrations as soon as the framework has matched a route
        # (Django's process_view, Flask's before_request), early enough that an error reported
        # from inside the view itself already carries them.
        self.transaction_name = None
        self.endpoint = None
        self._errored = False

    @classmethod
    def from_traceparent(cls, value):
        """Continues the caller's trace when the request arrived with a usable W3C traceparent
        header, remembering the caller's span as this request's remote parent; starts a fresh
        trace otherwise."""
        incoming = trace_parent.parse(value)
        if incoming is None:
            return cls()
        trace_id, parent_span_id = incoming
        return cls(trace_id=trace_id, parent_span_id=parent_span_id)

    def mark_errored(self):
        self._errored = True

    @property
    def errored(self):
        return self._errored
