"""A bounded, in-order trail of whatever happened recently in this logical unit of work: SQL
queries, the request/controller lifecycle, background jobs, plus anything added by hand via
forge_ops_tracker.add_breadcrumb, recorded automatically once the relevant integration's own hook
fires. Ported from gems/forge_ops_tracker/lib/forge_ops_tracker/breadcrumb_buffer.rb: the same
ring buffer contract (capped at Configuration.max_breadcrumbs, oldest entry dropped once full),
but this SDK backs it with a contextvars.ContextVar rather than Ruby's Thread.current.

That's a deliberate departure, not an oversight: this client already has to work under Celery
(worker processes, not always threads, and a thread pool that reuses one OS thread across many
unrelated task runs) as well as Flask/Django under a plain WSGI server (thread-per-request,
matching Ruby's own Puma model), and threading.local would silently do the wrong thing the moment
any future integration here runs under asyncio/ASGI, where several logical requests can interleave
on one OS thread as separate asyncio Tasks. contextvars.ContextVar is correct in every one of
those cases: a plain synchronous WSGI worker thread behaves exactly like a thread-local would (one
implicit Context per thread), but an asyncio Task also gets its own copy-on-write context the
moment it's created, so this stays correctly isolated there too with no separate mechanism needed
later. See forge_ops_tracker/__init__.py's own _breadcrumbs ContextVar, _start_breadcrumb_trail,
and _end_breadcrumb_trail for how a request/task actually gets (and loses) a fresh buffer.
"""

from datetime import datetime, timezone


class BreadcrumbBuffer:
    def __init__(self, configuration):
        self._configuration = configuration
        self._entries = []

    def add(self, category, message, level="info", data=None):
        # Read fresh on every add, not captured once at construction: the same reasoning
        # DeliveryQueue re-reads Configuration.queue_size on every push, so a config change from
        # init() takes effect on whatever's added next, not just on a buffer created afterward.
        max_size = max(self._configuration.max_breadcrumbs, 0)
        # Strict privacy never sends breadcrumbs, so they're never recorded either.
        if max_size == 0 or self._configuration.strict_privacy:
            return

        self._entries.append(
            {
                "category": str(category),
                "message": str(message),
                "level": str(level),
                "timestamp": _now_iso8601(),
                "data": data or {},
            }
        )
        overflow = len(self._entries) - max_size
        if overflow > 0:
            del self._entries[:overflow]

    def all(self):
        # A copy, not the internal list itself: a caller (ErrorSubscriber's own equivalent,
        # EventBuilder) must never be able to mutate this buffer's own state through what it reads
        # back, the same "returns a copy" contract Ruby's own #all already documents.
        return list(self._entries)


def _now_iso8601():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
