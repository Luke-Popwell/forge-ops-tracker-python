import json
import urllib.error

import pytest

from forge_ops_tracker import delivery_status
from forge_ops_tracker.client import Client
from forge_ops_tracker.configuration import Configuration


def _configuration(dsn="https://secret-key@tracker.example.com/api/v1/events"):
    config = Configuration()
    config.dsn = dsn
    return config


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patch_urlopen(monkeypatch, respond):
    """respond(request) -> _FakeResponse, or raises."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        return respond(request)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    return captured


def test_returns_delivered_on_a_successful_response(monkeypatch):
    _patch_urlopen(monkeypatch, lambda req: _FakeResponse(202))
    client = Client(_configuration())

    assert client.deliver({"exception_class": "RuntimeError"}) is delivery_status.DELIVERED


def test_returns_rejected_on_a_4xx_response(monkeypatch):
    def respond(req):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    _patch_urlopen(monkeypatch, respond)
    client = Client(_configuration())

    assert client.deliver({"exception_class": "RuntimeError"}) is delivery_status.REJECTED


@pytest.mark.parametrize(
    "code, expected",
    [
        (400, delivery_status.REJECTED),
        (403, delivery_status.REJECTED),
        (413, delivery_status.REJECTED),
        (422, delivery_status.REJECTED),
        (408, delivery_status.FAILED),
        (429, delivery_status.FAILED),
        (500, delivery_status.FAILED),
        (503, delivery_status.FAILED),
    ],
)
def test_tells_a_response_the_server_will_never_accept_from_one_worth_retrying(monkeypatch, code, expected):
    def respond(req):
        raise urllib.error.HTTPError(req.full_url, code, "nope", {}, None)

    _patch_urlopen(monkeypatch, respond)

    assert Client(_configuration()).deliver({"exception_class": "RuntimeError"}) is expected


def test_only_a_delivered_status_is_truthy_so_a_boolean_reading_of_the_result_still_holds():
    assert bool(delivery_status.DELIVERED) is True
    assert bool(delivery_status.REJECTED) is False
    assert bool(delivery_status.FAILED) is False
    assert delivery_status.from_result(True) is delivery_status.DELIVERED
    assert delivery_status.from_result(False) is delivery_status.FAILED
    assert delivery_status.from_result(None) is delivery_status.FAILED
    assert delivery_status.from_result(delivery_status.REJECTED) is delivery_status.REJECTED


def test_returns_failed_without_raising_when_the_request_raises_a_network_error(monkeypatch):
    def respond(req):
        raise urllib.error.URLError("getaddrinfo failed")

    _patch_urlopen(monkeypatch, respond)
    client = Client(_configuration())

    # Must not raise: the assertion below already proves that, but be
    # explicit that this is the property under test.
    result = client.deliver({"exception_class": "RuntimeError"})
    assert result is delivery_status.FAILED


def test_returns_failed_when_there_is_no_dsn_configured(monkeypatch):
    _patch_urlopen(monkeypatch, lambda req: _FakeResponse(202))
    client = Client(_configuration(dsn=None))

    assert client.deliver({"exception_class": "RuntimeError"}) is delivery_status.FAILED


def test_sends_the_api_key_as_a_bearer_token_and_the_payload_as_json(monkeypatch):
    captured = _patch_urlopen(monkeypatch, lambda req: _FakeResponse(202))
    client = Client(_configuration())

    client.deliver({"exception_class": "RuntimeError", "message": "boom"})

    sent_request = captured["request"]
    assert sent_request.get_header("Authorization") == "Bearer secret-key"
    assert json.loads(sent_request.data.decode("utf-8")) == {"exception_class": "RuntimeError", "message": "boom"}


def test_deliver_spans_posts_to_the_spans_uri_with_the_trace_id_and_spans(monkeypatch):
    captured = _patch_urlopen(monkeypatch, lambda req: _FakeResponse(202))
    client = Client(_configuration())

    result = client.deliver_spans("trace-1", [{"span_id": "s1", "name": "GET /checkout"}])

    assert result is delivery_status.DELIVERED
    sent_request = captured["request"]
    assert sent_request.full_url == "https://tracker.example.com/api/v1/spans"
    assert json.loads(sent_request.data.decode("utf-8")) == {
        "trace_id": "trace-1",
        "spans": [{"span_id": "s1", "name": "GET /checkout"}],
    }


def test_deliver_spans_returns_failed_when_there_is_no_dsn_configured():
    client = Client(_configuration(dsn=None))

    assert client.deliver_spans("trace-1", []) is delivery_status.FAILED
