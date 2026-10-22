import asyncio
import queue

import pytest
import requests

import forge_ops_tracker
from forge_ops_tracker.integrations.requests import _reset_for_testing, init_requests


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture(autouse=True)
def reset_requests_patch():
    yield
    _reset_for_testing()


def _init(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )
    return delivered


def _fake_transport_adapter(monkeypatch, status=200):
    """Stubs the one thing below Session.send that would otherwise make a real network call
    (HTTPAdapter.send), so this exercises the real requests.Session.send code path (URL/method
    parsing included) without needing an actual server or mocking Session.send itself, the very
    method this integration wraps."""
    import requests.adapters

    response = requests.Response()
    response.status_code = status
    response.request = None

    def fake_adapter_send(self, request, **kwargs):
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_adapter_send)


def test_records_an_http_span_named_by_method_and_host_only_never_the_path_or_query(monkeypatch):
    _init(monkeypatch)
    _fake_transport_adapter(monkeypatch, status=200)
    init_requests()

    token = forge_ops_tracker._start_span_trace()
    requests.get("https://api.stripe.com/v1/charges?customer=cus_123")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert len(buffer.spans) == 1
    span = buffer.spans[0]
    assert span["kind"] == "http"
    assert span["name"] == "GET api.stripe.com"
    assert span["data"] == {"status": 200}
    assert span["parent_span_id"] == buffer.root_span_id


def test_does_not_record_a_span_with_no_trace_currently_active(monkeypatch):
    _init(monkeypatch)
    _fake_transport_adapter(monkeypatch, status=200)
    init_requests()

    requests.get("https://api.stripe.com/v1/charges")

    assert forge_ops_tracker._spans.get() is None


def test_a_manually_wrapped_service_span_becomes_the_http_spans_parent(monkeypatch):
    _init(monkeypatch)
    _fake_transport_adapter(monkeypatch, status=200)
    init_requests()

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("PaymentService.charge", kind="service"):
        requests.get("https://api.stripe.com/v1/charges")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    service_span = next(s for s in buffer.spans if s["kind"] == "service")
    http_span = next(s for s in buffer.spans if s["kind"] == "http")
    assert http_span["parent_span_id"] == service_span["span_id"]


def test_init_requests_is_idempotent_and_never_double_records_a_span(monkeypatch):
    _init(monkeypatch)
    _fake_transport_adapter(monkeypatch, status=200)
    init_requests()
    init_requests()  # called twice, deliberately

    token = forge_ops_tracker._start_span_trace()
    requests.get("https://api.stripe.com/v1/charges")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert len(buffer.spans) == 1


# Trace propagation: the W3C traceparent header added to outbound calls made during a request.

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def _capturing_transport(monkeypatch):
    import requests.adapters

    sent = []

    def fake_adapter_send(self, request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_adapter_send)
    return sent


def _in_request(fn):
    state = forge_ops_tracker.RequestState(trace_id=TRACE_ID)
    request_token = forge_ops_tracker._start_request(state)
    span_token = forge_ops_tracker._start_span_trace()
    try:
        fn()
        return forge_ops_tracker._spans.get()
    finally:
        forge_ops_tracker._end_span_trace(span_token)
        forge_ops_tracker._end_request(request_token)


def test_adds_a_traceparent_naming_the_recorded_http_span_during_a_request(monkeypatch):
    _init(monkeypatch)
    sent = _capturing_transport(monkeypatch)
    init_requests()

    buffer = _in_request(lambda: requests.get("https://api.example.com/v1/orders"))

    version, trace_id, span_id, flags = sent[0].headers["traceparent"].split("-")
    assert (version, trace_id, flags) == ("00", TRACE_ID, "01")
    assert len(span_id) == 16
    assert buffer.spans[0]["span_id"] == span_id


def test_adds_the_header_even_with_track_tracing_off(monkeypatch):
    _init(monkeypatch)
    forge_ops_tracker.init(track_tracing=False)
    sent = _capturing_transport(monkeypatch)
    init_requests()

    _in_request(lambda: requests.get("https://api.example.com/v1/orders"))

    assert sent[0].headers["traceparent"].split("-")[1] == TRACE_ID


def test_never_adds_the_header_outside_a_request(monkeypatch):
    _init(monkeypatch)
    sent = _capturing_transport(monkeypatch)
    init_requests()

    requests.get("https://api.example.com/v1/orders")

    assert "traceparent" not in sent[0].headers


def test_never_replaces_a_traceparent_the_app_set_itself_whatever_its_case(monkeypatch):
    _init(monkeypatch)
    sent = _capturing_transport(monkeypatch)
    init_requests()
    own = f"00-{'a' * 32}-{'b' * 16}-01"

    _in_request(lambda: requests.get("https://api.example.com/v1/orders", headers={"TraceParent": own}))

    assert sent[0].headers["traceparent"] == own


def test_does_not_mutate_the_apps_own_prepared_request(monkeypatch):
    _init(monkeypatch)
    sent = _capturing_transport(monkeypatch)
    init_requests()
    prepared = requests.Request("GET", "https://api.example.com/v1/orders").prepare()

    def send_twice():
        with requests.Session() as session:
            session.send(prepared)
            session.send(prepared)

    _in_request(send_twice)

    assert "traceparent" not in prepared.headers
    assert sent[0].headers["traceparent"] != sent[1].headers["traceparent"]


def test_respects_propagate_traces_false(monkeypatch):
    _init(monkeypatch)
    forge_ops_tracker.init(propagate_traces=False)
    sent = _capturing_transport(monkeypatch)
    init_requests()

    buffer = _in_request(lambda: requests.get("https://api.example.com/v1/orders"))

    assert "traceparent" not in sent[0].headers
    assert len(buffer.spans) == 1  # the span is still recorded


def test_only_sends_the_header_to_trace_propagation_targets(monkeypatch):
    import re

    _init(monkeypatch)
    forge_ops_tracker.init(trace_propagation_targets=["example.com", re.compile(r"^10\.0\.")])
    sent = _capturing_transport(monkeypatch)
    init_requests()

    def calls():
        requests.get("https://api.example.com/v1/orders")
        requests.get("https://badexample.com/v1/orders")
        requests.get("http://10.0.0.5:8080/health")
        requests.get("https://api.stripe.com/v1/charges")

    _in_request(calls)

    assert ["traceparent" in request.headers for request in sent] == [True, False, True, False]


def test_never_adds_the_header_to_a_request_to_forgeops_itself(monkeypatch):
    _init(monkeypatch)
    sent = _capturing_transport(monkeypatch)
    init_requests()

    _in_request(lambda: requests.post("https://tracker.example.com/api/v1/events", json={}))

    assert "traceparent" not in sent[0].headers


def test_calls_from_concurrent_sibling_spans_each_nest_under_their_own_span(monkeypatch):
    _init(monkeypatch)
    _fake_transport_adapter(monkeypatch, status=200)
    init_requests()

    async def part(name, host):
        with forge_ops_tracker.span(name, kind="service"):
            await asyncio.sleep(0)  # let the sibling open its own span in between
            await asyncio.to_thread(requests.get, f"https://{host}/v1")

    async def run():
        token = forge_ops_tracker._start_span_trace()
        try:
            await asyncio.gather(part("PaymentService.charge", "api.stripe.com"), part("MailService.send", "api.mail.example"))
            return forge_ops_tracker._spans.get()
        finally:
            forge_ops_tracker._end_span_trace(token)

    buffer = asyncio.run(run())
    spans = {s["name"]: s for s in buffer.spans}

    assert spans["PaymentService.charge"]["parent_span_id"] == buffer.root_span_id
    assert spans["MailService.send"]["parent_span_id"] == buffer.root_span_id
    assert spans["GET api.stripe.com"]["parent_span_id"] == spans["PaymentService.charge"]["span_id"]
    assert spans["GET api.mail.example"]["parent_span_id"] == spans["MailService.send"]["span_id"]
