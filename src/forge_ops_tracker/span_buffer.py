"""Accumulates one request's own nested call tree in-process: the tracing analog to
BreadcrumbBuffer's per-request trail. A fresh buffer is created per request by the Django/Flask
tracing hooks (see integrations/django.py's ForgeOpsTrackerTracingMiddleware and
integrations/flask.py's own before_request/teardown_request pair), with the request's own
trace_id and a freshly generated root span_id, and read back once the request is over to decide whether this trace is
worth sending at all. Ported from gems/forge_ops_tracker/lib/forge_ops_tracker/span_buffer.rb.

The stack of open spans exists so nesting can be reconstructed correctly even though most of what
gets recorded here (a query, an outbound HTTP call) only has a start and a duration known after the
fact, not a real "this span is now open" moment the way a manually wrapped forge_ops_tracker.span()
block has. Whatever is on top of the stack at the moment a leaf span is recorded is that leaf's
real parent: the nearest currently open span, whether that's the request's own root or a
service-level span a customer wrapped their own code in. The root span_id is always the bottom of
the stack, specifically so every span recorded before the tracing hook itself finishes (which is
every one of them, since that hook only records the root span once the whole request, including
everything nested inside it, has already run) naturally nests under it without the root having to
exist as a real recorded span first.

The stack is kept per execution context (the _open_spans ContextVar below, holding an immutable
tuple), not as a list on the buffer. One trace can have several things running at once: asyncio
tasks the request started with gather or create_task, or threads run with a copy of its context
(asyncio.to_thread, contextvars.copy_context().run). Each copies the context it was started from,
so it sees the spans open at that moment as its parents, while a span() it opens itself only
changes its own copy and never becomes the parent of a sibling's spans, or leaks back into the
code that started it. A single shared list would interleave every sibling's pushes and pops.
"""

import contextvars
from datetime import timezone

# Re-exported from here, where they've always lived: W3C ids (32 and 16 lowercase hex characters,
# never all zeros) from trace_parent.py, the same ids a traceparent header carries.
from .trace_parent import generate_span_id, generate_trace_id

__all__ = ["SpanBuffer", "generate_span_id", "generate_trace_id"]

# (buffer, (span_id, ...)): the spans this context currently has open in that buffer, innermost
# last. Keyed by the buffer, so a context that has moved on to another trace (a reused worker
# thread, a nested _start_span_trace) never reads a previous trace's open spans as its own. None,
# the default, means nothing is open: the root is the parent.
_open_spans = contextvars.ContextVar("forge_ops_tracker_open_spans", default=None)


class SpanBuffer:
    def __init__(self, configuration, trace_id=None, parent_span_id=None):
        """trace_id/parent_span_id come from the request's own RequestState (see
        request_state.py) when there is one, never generated separately here, so a trace this
        sends and an error event from the same request always agree on which trace they belong
        to. parent_span_id is the calling service's span (from an incoming traceparent header),
        recorded as this request's root span's parent: the server treats a parent that isn't in
        this trace's own batch as remote, and nests this service's root under the caller's
        outgoing HTTP span."""
        self.trace_id = trace_id or generate_trace_id()
        self.root_span_id = generate_span_id()
        self._remote_parent_span_id = parent_span_id
        # Read once, at construction, not per-span: environment/release are properties of the
        # whole trace (the release running when this one request happened), not of any individual
        # span within it, the same values PerformanceFlusher's own samples already carry.
        self._environment = configuration.environment
        self._release = configuration.release
        self.spans = []
        self._root_duration_ms = None

    def _open(self):
        current = _open_spans.get()
        if current is not None and current[0] is self:
            return current[1]
        return (self.root_span_id,)

    @property
    def current_parent_id(self):
        return self._open()[-1]

    def push(self, span_id):
        """Opens span_id in the current context only. Returns the token pop() needs to close it."""
        return _open_spans.set((self, self._open() + (span_id,)))

    def pop(self, token):
        """Closes the span push() returned token for, restoring exactly what this context had open
        before it. A token can only be reset in the context that created it, so when a span is
        closed from somewhere else (a generator resumed in another task), the value from before
        the push is put back directly instead."""
        try:
            _open_spans.reset(token)
        except (ValueError, RuntimeError):
            previous = token.old_value
            _open_spans.set(None if previous is contextvars.Token.MISSING else previous)

    def record(self, span_id, name, kind, started_at, duration_ms, data=None, root=False):
        """root=True only for the one call that records the request's own root span: forces
        parent_span_id to the remote parent (None unless the request arrived with a traceparent
        header) explicitly, rather than reading the stack, whose bottom is the root itself (the
        root has no parent inside this service)."""
        parent_span_id = self._remote_parent_span_id if root else self.current_parent_id
        self.spans.append(
            {
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "name": name,
                "kind": kind,
                "started_at": _iso8601_millis(started_at),
                "duration_ms": duration_ms,
                "environment": self._environment,
                "release": self._release,
                "data": data or {},
            }
        )
        if root:
            self._root_duration_ms = duration_ms

    @property
    def root_recorded(self):
        """Whether the request's own root span has been recorded: an errored request's trace is
        only sent once it has, since spans with no root to hang from would render as a waterfall
        with no top."""
        return self._root_duration_ms is not None

    def is_slow(self, threshold_ms):
        """False (never sent) until the root span has actually been recorded: a request that
        somehow never reaches the end of its own tracing hook has no duration to compare against a
        threshold, so it's never mistakenly treated as slow."""
        return self._root_duration_ms is not None and self._root_duration_ms >= threshold_ms


def _iso8601_millis(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
