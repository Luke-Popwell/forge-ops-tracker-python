import asyncio
import contextvars
import json
import queue
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

import forge_ops_tracker


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patch_urlopen(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    return delivered


def _init(monkeypatch, **overrides):
    delivered = _patch_urlopen(monkeypatch)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        **overrides,
    )
    return delivered


def test_span_outside_any_trace_still_runs_the_wrapped_code_and_records_nothing():
    ran = []

    with forge_ops_tracker.span("PaymentService.charge"):
        ran.append(True)

    assert ran == [True]
    assert forge_ops_tracker._spans.get() is None


def test_span_outside_any_trace_never_raises_even_with_track_tracing_on():
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events")

    with forge_ops_tracker.span("PaymentService.charge"):
        pass  # must not raise


def test_span_records_into_the_currently_active_trace_parented_onto_the_root(monkeypatch):
    _init(monkeypatch)

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("PaymentService.charge", kind="service"):
        pass
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert len(buffer.spans) == 1
    entry = buffer.spans[0]
    assert entry["name"] == "PaymentService.charge"
    assert entry["kind"] == "service"
    assert entry["parent_span_id"] == buffer.root_span_id


def test_span_still_records_when_the_wrapped_code_raises_and_the_exception_propagates(monkeypatch):
    _init(monkeypatch)

    token = forge_ops_tracker._start_span_trace()
    try:
        with forge_ops_tracker.span("PaymentService.charge"):
            raise ValueError("card declined")
    except ValueError:
        pass
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert len(buffer.spans) == 1
    assert buffer.spans[0]["name"] == "PaymentService.charge"


def test_a_leaf_span_recorded_inside_a_manual_span_nests_under_it(monkeypatch):
    _init(monkeypatch)

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("PaymentService.charge") as _:
        import datetime

        forge_ops_tracker._record_leaf_span("GET api.stripe.com", kind="http", started_at=datetime.datetime.now(datetime.timezone.utc), duration_ms=5.0)
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    service_span = next(s for s in buffer.spans if s["name"] == "PaymentService.charge")
    http_span = next(s for s in buffer.spans if s["name"] == "GET api.stripe.com")
    assert http_span["parent_span_id"] == service_span["span_id"]


def test_span_is_usable_as_a_decorator(monkeypatch):
    _init(monkeypatch)

    @forge_ops_tracker.span("PaymentService.charge")
    def charge():
        return "charged"

    token = forge_ops_tracker._start_span_trace()
    result = charge()
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert result == "charged"
    assert buffer.spans[0]["name"] == "PaymentService.charge"


def test_span_is_a_no_op_when_track_tracing_is_off_even_with_an_active_trace(monkeypatch):
    _init(monkeypatch, track_tracing=False)

    # track_tracing=False means _start_span_trace itself returns None (see that function's own
    # doc), but simulate a trace somehow still being active to prove span() checks the flag
    # itself too, not just buffer presence.
    from forge_ops_tracker.span_buffer import SpanBuffer

    token = forge_ops_tracker._spans.set(SpanBuffer(forge_ops_tracker._state()[0]))
    with forge_ops_tracker.span("PaymentService.charge"):
        pass
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._spans.reset(token)

    assert buffer.spans == []


def test_start_span_trace_returns_none_when_track_tracing_is_off(monkeypatch):
    _init(monkeypatch, track_tracing=False)

    assert forge_ops_tracker._start_span_trace() is None


def test_a_slow_request_finish_span_trace_delivers_the_whole_trace_as_one_post(monkeypatch):
    delivered = _init(monkeypatch)

    import datetime

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("PaymentService.charge"):
        forge_ops_tracker._record_leaf_span("GET api.stripe.com", kind="http", started_at=datetime.datetime.now(datetime.timezone.utc), duration_ms=1200.0)
    forge_ops_tracker._finish_span_trace("CheckoutController#create", datetime.datetime.now(datetime.timezone.utc), 1800.0)
    forge_ops_tracker._end_span_trace(token)

    request = delivered.get(timeout=1)
    assert request.full_url.endswith("/api/v1/spans")
    payload = json.loads(request.data)
    assert len(payload["spans"]) == 3
    root = next(s for s in payload["spans"] if s["parent_span_id"] is None)
    assert root["name"] == "CheckoutController#create"
    assert root["kind"] == "controller"
    assert root["duration_ms"] == 1800.0


def test_a_fast_request_never_sends_anything(monkeypatch):
    delivered = _init(monkeypatch)

    import datetime

    token = forge_ops_tracker._start_span_trace()
    forge_ops_tracker._finish_span_trace("GET /ok", datetime.datetime.now(datetime.timezone.utc), 5.0)
    forge_ops_tracker._end_span_trace(token)

    forge_ops_tracker._span_queue_state()  # ensure the queue exists so this is a real "nothing pushed" check
    assert delivered.empty()


def test_exactly_one_span_in_the_delivered_trace_has_a_null_parent_span_id(monkeypatch):
    delivered = _init(monkeypatch)

    import datetime

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("PaymentService.charge"):
        forge_ops_tracker._record_leaf_span("GET api.stripe.com", kind="http", started_at=datetime.datetime.now(datetime.timezone.utc), duration_ms=1200.0)
    forge_ops_tracker._finish_span_trace("CheckoutController#create", datetime.datetime.now(datetime.timezone.utc), 1800.0)
    forge_ops_tracker._end_span_trace(token)

    payload = json.loads(delivered.get(timeout=1).data)
    roots = [s for s in payload["spans"] if s["parent_span_id"] is None]
    assert len(roots) == 1
    # And every other span's parent_span_id resolves to another span_id present in the same array.
    span_ids = {s["span_id"] for s in payload["spans"]}
    for s in payload["spans"]:
        if s["parent_span_id"] is not None:
            assert s["parent_span_id"] in span_ids


# span() per execution context: each asyncio task or context-copying thread has its own stack of
# open spans, so concurrent siblings never become each other's parents.


def _leaf(name):
    forge_ops_tracker._record_leaf_span(name, kind="redis", started_at=datetime.now(timezone.utc), duration_ms=1.0)


def _by_name(buffer):
    return {s["name"]: s for s in buffer.spans}


def _traced_async(fn):
    async def run():
        token = forge_ops_tracker._start_span_trace()
        try:
            await fn()
            return forge_ops_tracker._spans.get()
        finally:
            forge_ops_tracker._end_span_trace(token)

    return asyncio.run(run())


def test_sibling_tasks_each_opening_a_span_keep_their_own_leaves_and_hang_off_the_root(monkeypatch):
    _init(monkeypatch)

    async def part(name):
        with forge_ops_tracker.span(name):
            await asyncio.sleep(0)  # let the sibling open its own span in between
            _leaf(f"{name} leaf")
            await asyncio.sleep(0)

    async def run():
        await asyncio.gather(part("A"), part("B"))

    buffer = _traced_async(run)
    spans = _by_name(buffer)

    assert spans["A"]["parent_span_id"] == buffer.root_span_id
    assert spans["B"]["parent_span_id"] == buffer.root_span_id
    assert spans["A leaf"]["parent_span_id"] == spans["A"]["span_id"]
    assert spans["B leaf"]["parent_span_id"] == spans["B"]["span_id"]


def test_a_task_spawned_inside_a_span_nests_under_it_without_leaking_its_own_spans_back(monkeypatch):
    _init(monkeypatch)

    async def child():
        with forge_ops_tracker.span("child"):
            await asyncio.sleep(0)
            _leaf("child leaf")

    async def run():
        with forge_ops_tracker.span("outer"):
            task = asyncio.create_task(child())
            await asyncio.sleep(0)  # the child's span is open now, in its own context only
            _leaf("outer leaf")
            await task
        _leaf("after")

    buffer = _traced_async(run)
    spans = _by_name(buffer)

    assert spans["child"]["parent_span_id"] == spans["outer"]["span_id"]
    assert spans["child leaf"]["parent_span_id"] == spans["child"]["span_id"]
    assert spans["outer leaf"]["parent_span_id"] == spans["outer"]["span_id"]
    assert spans["after"]["parent_span_id"] == buffer.root_span_id


def test_nested_spans_in_one_task_nest_and_unwind_in_order(monkeypatch):
    _init(monkeypatch)

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("outer"):
        with forge_ops_tracker.span("inner"):
            _leaf("inner leaf")
        _leaf("outer leaf")
    _leaf("root leaf")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)
    spans = _by_name(buffer)

    assert spans["outer"]["parent_span_id"] == buffer.root_span_id
    assert spans["inner"]["parent_span_id"] == spans["outer"]["span_id"]
    assert spans["inner leaf"]["parent_span_id"] == spans["inner"]["span_id"]
    assert spans["outer leaf"]["parent_span_id"] == spans["outer"]["span_id"]
    assert spans["root leaf"]["parent_span_id"] == buffer.root_span_id


def test_a_span_that_raises_is_closed_so_the_next_leaf_goes_back_to_its_parent(monkeypatch):
    _init(monkeypatch)

    @forge_ops_tracker.span("decorated")
    def decorated():
        raise ValueError("card declined")

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("outer"):
        with pytest.raises(ValueError), forge_ops_tracker.span("inner"):
            raise ValueError("card declined")
        with pytest.raises(ValueError):
            decorated()
        _leaf("outer leaf")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)
    spans = _by_name(buffer)

    assert spans["inner"]["parent_span_id"] == spans["outer"]["span_id"]
    assert spans["decorated"]["parent_span_id"] == spans["outer"]["span_id"]
    assert spans["outer leaf"]["parent_span_id"] == spans["outer"]["span_id"]


def test_decorating_a_coroutine_function_covers_the_awaited_body_and_each_call_separately(monkeypatch):
    _init(monkeypatch)

    @forge_ops_tracker.span("CartService.load")
    async def load(n):
        await asyncio.sleep(0.01)
        _leaf(f"leaf {n}")
        if n == 2:
            raise ValueError("missing cart")
        return n

    async def run():
        results = await asyncio.gather(load(1), load(2), return_exceptions=True)
        assert results[0] == 1
        assert isinstance(results[1], ValueError)

    buffer = _traced_async(run)

    loads = [s for s in buffer.spans if s["name"] == "CartService.load"]
    leaves = _by_name(buffer)
    assert len(loads) == 2
    assert all(s["duration_ms"] >= 10 for s in loads)
    assert {s["parent_span_id"] for s in loads} == {buffer.root_span_id}
    assert {leaves["leaf 1"]["parent_span_id"], leaves["leaf 2"]["parent_span_id"]} == {s["span_id"] for s in loads}


def test_span_works_as_an_async_context_manager(monkeypatch):
    _init(monkeypatch)

    async def run():
        async with forge_ops_tracker.span("CartService.load"):
            _leaf("inside")
        _leaf("after")

    buffer = _traced_async(run)
    spans = _by_name(buffer)

    assert spans["inside"]["parent_span_id"] == spans["CartService.load"]["span_id"]
    assert spans["after"]["parent_span_id"] == buffer.root_span_id


def test_threads_run_with_a_copy_of_the_context_each_keep_their_own_spans(monkeypatch):
    _init(monkeypatch)

    def work(name):
        with forge_ops_tracker.span(name):
            _leaf(f"{name} leaf")

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("outer"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contextvars.copy_context().run, work, name) for name in ("A", "B")]
            for future in futures:
                future.result()
        _leaf("outer leaf")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)
    spans = _by_name(buffer)

    for name in ("A", "B"):
        assert spans[name]["parent_span_id"] == spans["outer"]["span_id"]
        assert spans[f"{name} leaf"]["parent_span_id"] == spans[name]["span_id"]
    assert spans["outer leaf"]["parent_span_id"] == spans["outer"]["span_id"]


def test_asyncio_to_thread_work_nests_under_the_span_that_started_it(monkeypatch):
    _init(monkeypatch)

    def blocking(name):
        with forge_ops_tracker.span(name):
            _leaf(f"{name} leaf")

    async def part(name):
        with forge_ops_tracker.span(f"{name} task"):
            await asyncio.to_thread(blocking, name)

    async def run():
        await asyncio.gather(part("A"), part("B"))

    buffer = _traced_async(run)
    spans = _by_name(buffer)

    for name in ("A", "B"):
        assert spans[f"{name} task"]["parent_span_id"] == buffer.root_span_id
        assert spans[name]["parent_span_id"] == spans[f"{name} task"]["span_id"]
        assert spans[f"{name} leaf"]["parent_span_id"] == spans[name]["span_id"]
