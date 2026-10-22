"""Redis span capture for redis-py. Call once, at startup:

    from forge_ops_tracker.integrations.redis import init_redis
    init_redis()

Not imported by forge_ops_tracker/__init__.py: redis-py is an optional dependency, so importing
this submodule specifically is what pulls it in, rather than every forge_ops_tracker user needing
redis installed. The same explicit opt-in shape init_requests() already has.

Wraps redis.Redis.execute_command, the one method every command a client sends goes through
(r.get(...), r.set(...), and any command built by hand with execute_command itself), so a span is
recorded per command. Pipeline overrides execute_command to queue commands instead of sending them,
so a queued command never reaches the wrapper above; Pipeline.execute is wrapped separately and
records the whole batch as one span, since that's the one real round trip. A pipeline that is
WATCHing keys sends its commands straight away instead of queueing them (the reads a transaction
decides on before calling multi()), through Pipeline.immediate_execute_command, which is wrapped
the same way as execute_command. Mirrors gems/forge_ops_tracker's own
Integrations::RedisClientTiming, which wraps RedisClient#call the same way for the same reason:
redis-py fires no event of its own to listen to.

The same three methods are wrapped on redis.asyncio's Redis and Pipeline, where they're coroutines:
the wrapper awaits the original inside the caller's own task, so the span is recorded against
whatever trace and open span() block that task sees. That's correct because the SDK's trace lives
in a contextvars.ContextVar (see forge_ops_tracker/__init__.py's _spans), not a threading.local:
an asyncio Task gets a copy of the context it was created in, so an awaited command, or one run in
a task the request spawned, reads the same trace as the code around it, and two requests
interleaving on one event loop never see each other's. RedisCluster (sync and asyncio) has its own
execute_command, separate from redis.Redis's, wrapped the same way. Each of these is skipped when
the installed redis-py doesn't have it, so an older release without redis.asyncio or RedisCluster
still gets everything else.

name is "Redis <COMMAND>" (the command verb alone, upcased: GET, SET, LPUSH, ...), never a key or
any other argument, since a Redis key can easily carry a customer's own id or other sensitive
value. A pipeline is "Redis PIPELINE", or "Redis MULTI" when it runs as a transaction (redis-py's
default), the same two operation names redis-py uses for them itself.

Also records a "redis" breadcrumb per command (level "error" when the command raised), like the
Ruby gem does. Both the span and the breadcrumb go through the same gated helpers every other
automatic source uses, so track_tracing and track_breadcrumbs turn them off, and a span is only
ever recorded inside a request currently being traced.
"""

import time
from datetime import datetime, timezone

import redis

from .. import _record_breadcrumb, _record_leaf_span, _state

# Each optional piece of redis-py on its own guard: RedisCluster arrived in redis-py 4.1 and
# redis.asyncio in 4.2, and a build can lack either without the rest of this module being any
# less useful.
try:
    import redis.cluster as _sync_cluster
except ImportError:  # pragma: no cover (depends on the installed redis-py)
    _sync_cluster = None
try:
    import redis.asyncio.client as _async_client
except ImportError:  # pragma: no cover
    _async_client = None
try:
    import redis.asyncio.cluster as _async_cluster
except ImportError:  # pragma: no cover
    _async_cluster = None

# (class, attribute, the class's own value before patching or None if it was inherited), one per
# method init_redis() patched, so _reset_for_testing can put back exactly what was there.
_patched = []


def init_redis():
    """Idempotent: calling this more than once (an app that inits it from more than one place)
    must never wrap redis-py's methods twice, which would otherwise double-record every command."""
    if _patched:
        return
    _patch(redis.Redis, "execute_command", _command_wrapper)
    _patch(redis.client.Pipeline, "execute", _pipeline_wrapper)
    _patch(redis.client.Pipeline, "immediate_execute_command", _command_wrapper)
    if _sync_cluster is not None:
        _patch(getattr(_sync_cluster, "RedisCluster", None), "execute_command", _command_wrapper)
    if _async_client is not None:
        _patch(getattr(_async_client, "Redis", None), "execute_command", _async_command_wrapper)
        _patch(getattr(_async_client, "Pipeline", None), "execute", _async_pipeline_wrapper)
        _patch(getattr(_async_client, "Pipeline", None), "immediate_execute_command", _async_command_wrapper)
    if _async_cluster is not None:
        _patch(getattr(_async_cluster, "RedisCluster", None), "execute_command", _async_command_wrapper)


def _patch(owner, attribute, make_wrapper):
    original = getattr(owner, attribute, None) if owner is not None else None
    if original is None:
        return
    _patched.append((owner, attribute, owner.__dict__.get(attribute)))
    setattr(owner, attribute, make_wrapper(original))


def _command_wrapper(original):
    def execute_command(self, *args, **options):
        return _timed(_command_name(args), original, self, *args, **options)

    return execute_command


def _pipeline_wrapper(original):
    def execute(self, *args, **kwargs):
        name = _pipeline_name(self)
        if name is None:
            return original(self, *args, **kwargs)
        return _timed(name, original, self, *args, **kwargs)

    return execute


def _async_command_wrapper(original):
    async def execute_command(self, *args, **options):
        return await _timed_async(_command_name(args), original, self, *args, **options)

    return execute_command


def _async_pipeline_wrapper(original):
    async def execute(self, *args, **kwargs):
        name = _pipeline_name(self)
        if name is None:
            return await original(self, *args, **kwargs)
        return await _timed_async(name, original, self, *args, **kwargs)

    return execute


def _pipeline_name(pipe):
    """None when execute() makes no round trip: redis-py returns [] without touching the connection
    for a pipeline with nothing queued, unless it's WATCHing keys, in which case it still sends an
    empty MULTI/EXEC to find out whether the watched keys changed."""
    if not pipe.command_stack and not getattr(pipe, "watching", False):
        return None
    # redis.asyncio's Pipeline keeps the flag as is_transaction; there, "transaction" is the
    # Redis.transaction() method it inherits, which is always truthy.
    transaction = getattr(pipe, "is_transaction", None)
    if transaction is None:
        transaction = pipe.transaction
    operation = "MULTI" if (transaction or pipe.explicit_transaction) else "PIPELINE"
    return f"Redis {operation}"


def _command_name(args):
    """The span name, built from the command's first argument only. redis-py passes some commands with
    their subcommand in that same first argument ("CLIENT SETNAME"), so only its first word is
    kept, matching what the Ruby gem records for the same command."""
    if not args:
        return "Redis"
    verb = args[0]
    if isinstance(verb, (bytes, bytearray, memoryview)):
        verb = bytes(verb).decode("utf-8", "replace")
    words = str(verb).split()
    return f"Redis {words[0].upper()}" if words else "Redis"


def _timed(name, original, client, *args, **kwargs):
    started_at = datetime.now(timezone.utc)
    start = time.monotonic()
    raised = False
    try:
        return original(client, *args, **kwargs)
    except Exception:
        raised = True
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        _record(name, started_at, duration_ms, raised)


async def _timed_async(name, original, client, *args, **kwargs):
    """_timed above for a coroutine. Awaited in the caller's own task, never scheduled as a task of
    its own, so _record reads the same trace (and the same open span() block) the caller does."""
    started_at = datetime.now(timezone.utc)
    start = time.monotonic()
    raised = False
    try:
        return await original(client, *args, **kwargs)
    except Exception:
        raised = True
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        _record(name, started_at, duration_ms, raised)


def _record(name, started_at, duration_ms, raised):
    """Never raises: this runs inside the app's own Redis call, and a failure to record a span or a
    breadcrumb must never replace the command's own result or exception."""
    try:
        _record_breadcrumb(name, category="redis", level="error" if raised else "info", data={"duration_ms": round(duration_ms, 1)})
        _record_leaf_span(name, kind="redis", started_at=started_at, duration_ms=duration_ms)
    except Exception as e:  # noqa: BLE001 (runs inside the app's own Redis call)
        _state()[0].logger.debug("[forge_ops_tracker] redis span failed: %s: %s", type(e).__name__, e)


def _reset_for_testing():
    """Not part of the public API: undoes init_redis()'s own monkeypatch between test cases, the
    same shape integrations/requests.py's own _reset_for_testing already has."""
    while _patched:
        owner, attribute, original = _patched.pop()
        if original is None:
            delattr(owner, attribute)
        else:
            setattr(owner, attribute, original)
