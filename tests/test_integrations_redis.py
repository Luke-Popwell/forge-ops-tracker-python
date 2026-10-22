import asyncio
import queue

import pytest

redis = pytest.importorskip("redis")
fakeredis = pytest.importorskip("fakeredis")
fakeredis_aioredis = pytest.importorskip("fakeredis.aioredis")

import forge_ops_tracker
from forge_ops_tracker.integrations.redis import (
    _command_name,
    _reset_for_testing,
    init_redis,
)


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture(autouse=True)
def reset_redis_patch():
    yield
    _reset_for_testing()


def _init(monkeypatch, **options):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        **options,
    )
    return delivered


def _client():
    """A real redis.Redis subclass whose connection talks to an in-memory server instead of a
    socket, so every command runs through redis-py's own execute_command and Pipeline.execute (the
    two methods this integration wraps) without needing a live Redis."""
    return fakeredis.FakeRedis(server=fakeredis.FakeServer())


def _traced(fn):
    token = forge_ops_tracker._start_span_trace()
    try:
        fn()
        return forge_ops_tracker._spans.get()
    finally:
        forge_ops_tracker._end_span_trace(token)


def test_records_a_redis_span_per_command_named_by_the_command_only_never_its_key_or_value(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    buffer = _traced(lambda: (client.set("session:cus_123", "secret-token"), client.get("session:cus_123")))

    assert [s["name"] for s in buffer.spans] == ["Redis SET", "Redis GET"]
    for span in buffer.spans:
        assert span["kind"] == "redis"
        assert span["data"] == {}
        assert span["parent_span_id"] == buffer.root_span_id
        assert span["duration_ms"] >= 0
        assert "cus_123" not in repr(span)
        assert "secret-token" not in repr(span)


def test_does_not_record_a_span_with_no_trace_currently_active(monkeypatch):
    _init(monkeypatch)
    init_redis()

    _client().set("key", "value")

    assert forge_ops_tracker._spans.get() is None


def test_records_nothing_with_track_tracing_off(monkeypatch):
    _init(monkeypatch, track_tracing=False)
    init_redis()
    client = _client()

    token = forge_ops_tracker._start_span_trace()
    client.set("key", "value")
    forge_ops_tracker._end_span_trace(token)

    assert token is None


def test_a_manually_wrapped_service_span_becomes_the_redis_spans_parent(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    def run():
        with forge_ops_tracker.span("CartService.load", kind="service"):
            client.hgetall("cart:42")

    buffer = _traced(run)

    service_span = next(s for s in buffer.spans if s["kind"] == "service")
    redis_span = next(s for s in buffer.spans if s["kind"] == "redis")
    assert redis_span["name"] == "Redis HGETALL"
    assert redis_span["parent_span_id"] == service_span["span_id"]


def test_a_transactional_pipeline_is_one_multi_span_not_one_per_queued_command(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    def run():
        pipe = client.pipeline()
        pipe.set("a", "1")
        pipe.incr("counter")
        pipe.execute()

    buffer = _traced(run)

    assert [s["name"] for s in buffer.spans] == ["Redis MULTI"]
    assert buffer.spans[0]["kind"] == "redis"


def test_a_non_transactional_pipeline_is_one_pipeline_span(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    def run():
        with client.pipeline(transaction=False) as pipe:
            pipe.get("a")
            pipe.get("b")
            pipe.execute()

    buffer = _traced(run)

    assert [s["name"] for s in buffer.spans] == ["Redis PIPELINE"]


def test_an_empty_pipeline_records_nothing(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    buffer = _traced(lambda: client.pipeline().execute())

    assert buffer.spans == []


def test_a_command_that_raises_still_records_its_span_and_the_error_reaches_the_app(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()
    client.set("name", "not a list")

    def run():
        with pytest.raises(redis.exceptions.ResponseError):
            client.lpush("name", "x")

    buffer = _traced(run)

    assert [s["name"] for s in buffer.spans] == ["Redis LPUSH"]


def test_records_a_redis_breadcrumb_with_error_level_when_the_command_raised(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    token = forge_ops_tracker._start_breadcrumb_trail()
    client.set("name", "not a list")
    with pytest.raises(redis.exceptions.ResponseError):
        client.lpush("name", "x")
    trail = forge_ops_tracker._breadcrumbs.get().all()
    forge_ops_tracker._end_breadcrumb_trail(token)

    assert [(c["message"], c["category"], c["level"]) for c in trail] == [
        ("Redis SET", "redis", "info"),
        ("Redis LPUSH", "redis", "error"),
    ]
    assert "duration_ms" in trail[0]["data"]


def test_init_redis_is_idempotent_and_never_double_records_a_span(monkeypatch):
    _init(monkeypatch)
    init_redis()
    init_redis()  # called twice, deliberately
    client = _client()

    buffer = _traced(lambda: client.get("key"))

    assert len(buffer.spans) == 1


def test_reset_restores_redis_pys_own_methods(monkeypatch):
    patched = [
        (redis.Redis, "execute_command"),
        (redis.client.Pipeline, "execute"),
        (redis.client.Pipeline, "immediate_execute_command"),
        (redis.cluster.RedisCluster, "execute_command"),
        (redis.asyncio.Redis, "execute_command"),
        (redis.asyncio.client.Pipeline, "execute"),
        (redis.asyncio.client.Pipeline, "immediate_execute_command"),
        (redis.asyncio.cluster.RedisCluster, "execute_command"),
    ]
    originals = [(owner, attribute, owner.__dict__.get(attribute)) for owner, attribute in patched]
    init_redis()
    for owner, attribute, original in originals:
        assert owner.__dict__.get(attribute) is not original

    _reset_for_testing()

    for owner, attribute, original in originals:
        assert owner.__dict__.get(attribute) is original


def test_a_failure_to_record_never_breaks_the_command(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    def boom(*args, **kwargs):
        raise RuntimeError("recording failed")

    monkeypatch.setattr("forge_ops_tracker.integrations.redis._record_leaf_span", boom)

    token = forge_ops_tracker._start_span_trace()
    try:
        assert client.set("key", "value") is True
        assert client.get("key") == b"value"
    finally:
        forge_ops_tracker._end_span_trace(token)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("GET", "key"), "Redis GET"),
        (("get", "key"), "Redis GET"),
        ((b"hset", "key", "field", "value"), "Redis HSET"),
        (("CLIENT SETNAME", "worker-1"), "Redis CLIENT"),
        ((), "Redis"),
    ],
)
def test_command_name_is_the_upcased_verb_alone(args, expected):
    assert _command_name(args) == expected


def test_commands_sent_while_a_pipeline_is_watching_are_their_own_spans(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()
    client.set("balance", "10")

    def run():
        with client.pipeline() as pipe:
            pipe.watch("balance")
            balance = int(pipe.get("balance"))
            pipe.multi()
            pipe.set("balance", balance - 1)
            pipe.execute()

    buffer = _traced(run)

    assert [s["name"] for s in buffer.spans] == ["Redis WATCH", "Redis GET", "Redis MULTI"]
    for span in buffer.spans:
        assert span["kind"] == "redis"
        assert span["parent_span_id"] == buffer.root_span_id
        assert "balance" not in repr(span)


def test_a_watching_pipeline_with_nothing_queued_still_records_its_round_trip(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _client()

    def run():
        with client.pipeline() as pipe:
            pipe.watch("balance")
            pipe.execute()

    buffer = _traced(run)

    assert [s["name"] for s in buffer.spans] == ["Redis WATCH", "Redis MULTI"]


# redis.asyncio. Each test runs its own event loop with asyncio.run, and starts the trace from
# inside it, the way an async request handler's own middleware would.


def _async_client():
    """A real redis.asyncio.Redis subclass over the same in-memory server as _client above. From
    fakeredis.aioredis rather than the top-level FakeAsyncRedis name, which only newer fakeredis
    releases export."""
    return fakeredis_aioredis.FakeRedis(server=fakeredis.FakeServer())


def _traced_async(fn):
    async def run():
        token = forge_ops_tracker._start_span_trace()
        try:
            await fn()
            return forge_ops_tracker._spans.get()
        finally:
            forge_ops_tracker._end_span_trace(token)

    return asyncio.run(run())


def test_records_a_redis_span_per_awaited_command_never_its_key_or_value(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def run():
        await client.set("session:cus_123", "secret-token")
        assert await client.get("session:cus_123") == b"secret-token"

    buffer = _traced_async(run)

    assert [s["name"] for s in buffer.spans] == ["Redis SET", "Redis GET"]
    for span in buffer.spans:
        assert span["kind"] == "redis"
        assert span["data"] == {}
        assert span["parent_span_id"] == buffer.root_span_id
        assert "cus_123" not in repr(span)
        assert "secret-token" not in repr(span)


def test_an_awaited_command_nests_under_the_span_block_its_coroutine_is_in(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def run():
        with forge_ops_tracker.span("CartService.load", kind="service"):
            await asyncio.sleep(0)
            await client.hgetall("cart:42")
        await client.get("after")

    buffer = _traced_async(run)

    service_span = next(s for s in buffer.spans if s["kind"] == "service")
    inside, after = (s for s in buffer.spans if s["kind"] == "redis")
    assert inside["name"] == "Redis HGETALL"
    assert inside["parent_span_id"] == service_span["span_id"]
    assert after["parent_span_id"] == buffer.root_span_id


def test_commands_from_tasks_a_request_spawns_land_in_that_requests_trace(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def run():
        with forge_ops_tracker.span("CartService.load", kind="service"):
            await asyncio.gather(client.get("a"), client.get("b"), asyncio.create_task(client.get("c")))

    buffer = _traced_async(run)

    service_span = next(s for s in buffer.spans if s["kind"] == "service")
    redis_spans = [s for s in buffer.spans if s["kind"] == "redis"]
    assert len(redis_spans) == 3
    assert {s["parent_span_id"] for s in redis_spans} == {service_span["span_id"]}


def test_sibling_tasks_each_in_their_own_span_keep_their_own_commands(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def part(name, key):
        with forge_ops_tracker.span(name, kind="service"):
            await asyncio.sleep(0)  # let the sibling open its own span in between
            await client.execute_command(key, "key")

    async def run():
        await asyncio.gather(part("A", "GET"), part("B", "TTL"))

    buffer = _traced_async(run)
    spans = {s["name"]: s for s in buffer.spans}

    assert spans["A"]["parent_span_id"] == buffer.root_span_id
    assert spans["B"]["parent_span_id"] == buffer.root_span_id
    assert spans["Redis GET"]["parent_span_id"] == spans["A"]["span_id"]
    assert spans["Redis TTL"]["parent_span_id"] == spans["B"]["span_id"]


def test_requests_interleaving_on_one_event_loop_each_get_only_their_own_commands(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def request(verbs):
        token = forge_ops_tracker._start_span_trace()
        try:
            for verb in verbs:
                await asyncio.sleep(0)  # let the other request run in between
                await client.execute_command(verb, "key")
            return forge_ops_tracker._spans.get()
        finally:
            forge_ops_tracker._end_span_trace(token)

    async def both():
        return await asyncio.gather(request(["GET", "STRLEN", "EXISTS"]), request(["TTL", "TYPE"]))

    first, second = asyncio.run(both())

    assert [s["name"] for s in first.spans] == ["Redis GET", "Redis STRLEN", "Redis EXISTS"]
    assert [s["name"] for s in second.spans] == ["Redis TTL", "Redis TYPE"]
    assert {s["parent_span_id"] for s in first.spans} == {first.root_span_id}
    assert {s["parent_span_id"] for s in second.spans} == {second.root_span_id}


def test_does_not_record_an_async_span_with_no_trace_currently_active(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def run():
        await client.set("key", "value")
        return forge_ops_tracker._spans.get()

    assert asyncio.run(run()) is None


def test_an_async_pipeline_is_one_span_multi_or_pipeline(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def run():
        async with client.pipeline() as pipe:
            await pipe.set("a", "1").incr("counter").execute()
        async with client.pipeline(transaction=False) as pipe:
            await pipe.get("a").get("b").execute()
        await client.pipeline().execute()  # empty, so nothing is sent

    buffer = _traced_async(run)

    assert [s["name"] for s in buffer.spans] == ["Redis MULTI", "Redis PIPELINE"]


def test_async_commands_sent_while_a_pipeline_is_watching_are_their_own_spans(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    async def run():
        await client.set("balance", "10")
        async with client.pipeline() as pipe:
            await pipe.watch("balance")
            balance = int(await pipe.get("balance"))
            pipe.multi()
            pipe.set("balance", balance - 1)
            await pipe.execute()

    buffer = _traced_async(run)

    assert [s["name"] for s in buffer.spans] == ["Redis SET", "Redis WATCH", "Redis GET", "Redis MULTI"]


def test_an_async_command_that_raises_records_its_span_and_an_error_breadcrumb(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()
    trail = []

    async def run():
        token = forge_ops_tracker._start_breadcrumb_trail()
        try:
            await client.set("name", "not a list")
            with pytest.raises(redis.exceptions.ResponseError):
                await client.lpush("name", "x")
            trail.extend(forge_ops_tracker._breadcrumbs.get().all())
        finally:
            forge_ops_tracker._end_breadcrumb_trail(token)

    buffer = _traced_async(run)

    assert [s["name"] for s in buffer.spans] == ["Redis SET", "Redis LPUSH"]
    assert [(c["message"], c["level"]) for c in trail] == [("Redis SET", "info"), ("Redis LPUSH", "error")]


def test_a_failure_to_record_never_breaks_an_async_command(monkeypatch):
    _init(monkeypatch)
    init_redis()
    client = _async_client()

    def boom(*args, **kwargs):
        raise RuntimeError("recording failed")

    monkeypatch.setattr("forge_ops_tracker.integrations.redis._record_leaf_span", boom)

    async def run():
        assert await client.set("key", "value") is True
        assert await client.get("key") == b"value"

    _traced_async(run)


# RedisCluster. fakeredis has no cluster, so these swap the class's own execute_command for a stub
# that answers without a connection; init_redis() then wraps the stub exactly as it would the real
# method, and a command method like cluster.get(...) still goes through it.


@pytest.fixture
def stub_cluster_commands():
    classes = [redis.cluster.RedisCluster, redis.asyncio.cluster.RedisCluster]
    originals = [cls.__dict__["execute_command"] for cls in classes]

    def execute_command(self, *args, **kwargs):
        if args[0] == "LPUSH":
            raise redis.exceptions.ResponseError("WRONGTYPE")
        return b"value"

    async def async_execute_command(self, *args, **kwargs):
        return execute_command(self, *args, **kwargs)

    redis.cluster.RedisCluster.execute_command = execute_command
    redis.asyncio.cluster.RedisCluster.execute_command = async_execute_command
    yield
    _reset_for_testing()  # before restoring the originals, so it doesn't put the stubs back
    for cls, original in zip(classes, originals):
        cls.execute_command = original


def test_records_a_redis_cluster_span_per_command(monkeypatch, stub_cluster_commands):
    _init(monkeypatch)
    init_redis()
    cluster = object.__new__(redis.cluster.RedisCluster)

    def run():
        assert cluster.get("session:cus_123") == b"value"
        with pytest.raises(redis.exceptions.ResponseError):
            cluster.lpush("session:cus_123", "x")

    buffer = _traced(run)

    assert [s["name"] for s in buffer.spans] == ["Redis GET", "Redis LPUSH"]
    for span in buffer.spans:
        assert span["kind"] == "redis"
        assert span["parent_span_id"] == buffer.root_span_id
        assert "cus_123" not in repr(span)


def test_records_an_async_redis_cluster_span_per_command(monkeypatch, stub_cluster_commands):
    _init(monkeypatch)
    init_redis()
    cluster = object.__new__(redis.asyncio.cluster.RedisCluster)

    async def run():
        with forge_ops_tracker.span("CartService.load", kind="service"):
            assert await cluster.get("session:cus_123") == b"value"

    buffer = _traced_async(run)

    service_span = next(s for s in buffer.spans if s["kind"] == "service")
    redis_span = next(s for s in buffer.spans if s["kind"] == "redis")
    assert redis_span["name"] == "Redis GET"
    assert redis_span["parent_span_id"] == service_span["span_id"]
