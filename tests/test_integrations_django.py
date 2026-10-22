import json
import queue

import pytest
from django.test import Client as DjangoTestClient

import forge_ops_tracker


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_reports_the_exception_and_still_lets_django_handle_it(monkeypatch):
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

    client = DjangoTestClient(raise_request_exception=False)
    response = client.get("/throw")

    # The middleware only observes: Django's own handling (a 500 here,
    # since DEBUG is off in the test settings) behaves exactly as if the
    # middleware weren't installed.
    assert response.status_code == 500

    request = delivered.get(timeout=1)
    assert request.get_header("Authorization") == "Bearer key"


def test_attaches_the_authenticated_user_to_a_reported_exception(monkeypatch):
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

    client = DjangoTestClient(raise_request_exception=False)
    client.get(
        "/throw",
        HTTP_X_TEST_USER_ID="42",
        HTTP_X_TEST_USER_EMAIL="ada@example.com",
    )

    payload = _next_delivery_to(delivered, "/events")
    assert payload["user"] == {"id": "42", "email": "ada@example.com"}


def test_does_not_attach_a_user_for_an_unauthenticated_request(monkeypatch):
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

    client = DjangoTestClient(raise_request_exception=False)
    client.get("/throw")  # no X-Test-User-Id header: FakeAuthMiddleware sets an AnonymousUser

    payload = _next_delivery_to(delivered, "/events")
    assert "user" not in payload


def test_an_explicit_user_argument_still_overrides_auto_detection(monkeypatch):
    # capture_exception's own user= kwarg, or a manual set_user() call from application code
    # after this middleware ran, must still win: the same "compose, don't replace" contract
    # gems/forge_ops_tracker's own Warden integration documents.
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

    try:
        raise ValueError("boom")
    except ValueError as exc:
        forge_ops_tracker.set_user(id="42")
        forge_ops_tracker.capture_exception(exc, user={"id": "99"})

    payload = _next_delivery_to(delivered, "/events")
    assert payload["user"] == {"id": "99"}


def test_counts_a_successful_request_as_a_crash_free_session(monkeypatch):
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

    client = DjangoTestClient()
    response = client.get("/ok")
    assert response.status_code == 200

    # The background thread's own interval (60s by default) is too slow for a test; force the
    # flush directly instead of waiting for it, the same way the flusher's own tests do.
    forge_ops_tracker._session_state()[1].flush()

    request = delivered.get(timeout=1)
    assert request.full_url.endswith("/api/v1/session_checkins")

    payload = json.loads(request.data)
    assert payload["sessions_count"] == 1
    assert payload["crashed_sessions_count"] == 0


def test_counts_a_crashed_request_as_a_crashed_session(monkeypatch):
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

    client = DjangoTestClient(raise_request_exception=False)
    client.get("/throw")

    forge_ops_tracker._session_state()[1].flush()

    # Three requests were delivered above: the error event (from ForgeOpsTrackerMiddleware), the
    # errored request's trace, and the session checkin; find the session checkin specifically
    # rather than assuming order (or that it is among the first two).

    session_checkin = _next_delivery_to(delivered, "/api/v1/session_checkins")

    assert session_checkin is not None
    assert session_checkin["sessions_count"] == 1
    assert session_checkin["crashed_sessions_count"] == 1


def test_times_a_request_against_a_parameterized_route_using_the_url_pattern_not_the_raw_path(monkeypatch):
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

    client = DjangoTestClient()
    response = client.get("/users/42/")
    assert response.status_code == 200

    forge_ops_tracker._performance_state()[1].flush()

    request = delivered.get(timeout=1)
    assert request.full_url.endswith("/api/v1/performance_samples")

    payload = json.loads(request.data)
    assert len(payload["samples"]) == 1
    # The URL pattern, not "GET /users/42/": a distinct user id must not explode into its own
    # separate transaction the way the literal path would.
    assert payload["samples"][0]["transaction_name"] == "GET users/<int:user_id>/"
    assert payload["samples"][0]["request_count"] == 1
    assert payload["samples"][0]["kind"] == "controller"


@pytest.mark.django_db
def test_times_a_query_run_during_the_request_as_its_own_kind_query_sample(monkeypatch):
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

    client = DjangoTestClient()
    response = client.get("/run-a-query")
    assert response.status_code == 200

    forge_ops_tracker._performance_state()[1].flush()

    request = delivered.get(timeout=1)
    payload = json.loads(request.data)

    query_sample = next(s for s in payload["samples"] if s["kind"] == "query")
    assert query_sample["request_count"] == 1
    # SELECT 1 has no FROM clause, so this exercises _query_transaction_name's own fallback path
    # (just the first keyword) rather than its table-name extraction, which is covered directly
    # against realistic SQL in test_query_naming.py instead.
    assert query_sample["transaction_name"] == "SELECT"

    controller_sample = next(s for s in payload["samples"] if s["kind"] == "controller")
    assert controller_sample["transaction_name"] == "GET run-a-query"


@pytest.mark.django_db
def test_records_controller_and_query_breadcrumbs_into_the_active_trail(monkeypatch):
    # Django's own convert_exception_to_response wraps *each* middleware layer individually (see
    # ForgeOpsTrackerSessionTrackingMiddleware's own comment): an unhandled view exception is
    # already resolved into a response, and process_exception already called, entirely *inside*
    # ForgeOpsTrackerPerformanceTrackingMiddleware's own self.get_response(request) call, before
    # that middleware's own finally (where the controller breadcrumb is recorded) ever runs. So a
    # controller breadcrumb can never show up on that exact same request's own reported error (the
    # query breadcrumb still can, since it's recorded synchronously mid-view, well before any
    # exception later in that same view propagates; see
    # test_attaches_the_automatic_query_breadcrumb_and_a_manually_added_one_to_the_same_error
    # below). This exercises the middleware pair directly instead, the same way
    # gems/forge_ops_tracker's own breadcrumb_context_spec.rb tests its middleware against a bare
    # lambda app rather than a full Rails request, to verify what actually gets recorded
    # independent of that Django-specific exception-routing timing gap.
    from django.db import connection
    from django.http import HttpResponse
    from django.test import RequestFactory

    from forge_ops_tracker.integrations.django import (
        ForgeOpsTrackerBreadcrumbContextMiddleware,
        ForgeOpsTrackerPerformanceTrackingMiddleware,
    )

    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    seen_buffer = []

    def get_response(request):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        # The same live BreadcrumbBuffer object PerformanceTrackingMiddleware's own finally (which
        # hasn't run yet) will go on to add the controller breadcrumb to: grabbing the reference
        # here, not a snapshot, is what lets this test see that later mutation too.
        seen_buffer.append(forge_ops_tracker._breadcrumbs.get())
        return HttpResponse("ok", status=200)

    middleware = ForgeOpsTrackerBreadcrumbContextMiddleware(ForgeOpsTrackerPerformanceTrackingMiddleware(get_response))
    request = RequestFactory().get("/some-path")

    middleware(request)

    entries = seen_buffer[0].all()
    assert [e["category"] for e in entries] == ["query", "controller"]
    assert entries[0]["message"] == "SELECT"
    controller_entry = entries[1]
    assert controller_entry["message"] == "GET /some-path"
    assert controller_entry["level"] == "info"
    assert controller_entry["data"] == {"status": 200, "path": "/some-path"}
    # And cleared again once the whole call (including the wrapping BreadcrumbContextMiddleware)
    # has returned, the same as every other request.
    assert forge_ops_tracker._breadcrumbs.get() is None


@pytest.mark.django_db
def test_attaches_the_automatic_query_breadcrumb_and_a_manually_added_one_to_the_same_error(monkeypatch):
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

    client = DjangoTestClient(raise_request_exception=False)
    client.get("/throw-with-breadcrumb")

    payload = _next_delivery_to(delivered, "/events")

    categories = [b["category"] for b in payload["breadcrumbs"]]
    assert categories == ["query", "custom"]
    assert payload["breadcrumbs"][0]["message"] == "SELECT"
    assert payload["breadcrumbs"][1]["message"] == "about to explode"


@pytest.mark.django_db
def test_does_not_attach_breadcrumbs_from_one_request_to_a_later_unrelated_requests_error(monkeypatch):
    # Two sequential requests handled on the same Django test client (and, under a real WSGI
    # server, potentially the same reused worker thread): the second request's own error must
    # never see the first request's breadcrumbs.
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

    client = DjangoTestClient(raise_request_exception=False)
    client.get("/throw-with-breadcrumb")
    client.get("/throw")

    # A single DeliveryQueue with one worker thread delivers in the same order events were
    # pushed, so the first item off the queue is /throw-with-breadcrumb's own event and the
    # second is /throw's; the second must never carry the first request's own breadcrumbs. Each
    # errored request's trace goes out from a separate worker thread, in between at any point,
    # so only deliveries to the events endpoint are read here.
    first_event = _next_delivery_to(delivered, "/events")
    second_event = _next_delivery_to(delivered, "/events")

    assert first_event.get("breadcrumbs", []) != []
    assert second_event.get("breadcrumbs", []) == []


@pytest.mark.django_db
def test_automatic_breadcrumbs_are_suppressed_when_track_breadcrumbs_is_off_but_manual_ones_still_work(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)

    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        track_breadcrumbs=False,
    )

    client = DjangoTestClient(raise_request_exception=False)
    client.get("/throw-with-breadcrumb")

    payload = _next_delivery_to(delivered, "/events")
    # The automatic query breadcrumb is gone (track_breadcrumbs=False), but the manual
    # add_breadcrumb() call in the view still worked: that API is never gated by this flag.
    assert [b["category"] for b in payload["breadcrumbs"]] == ["custom"]


@pytest.mark.django_db
def test_a_slow_request_delivers_a_trace_with_the_root_service_and_database_spans_correctly_nested(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)

    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        release="v2.41.0",
        # 0 so this real (fast) test request still counts as "slow" and gets delivered,
        # deterministically, without actually having to sleep for a full second.
        trace_capture_threshold_ms=0,
    )

    client = DjangoTestClient()
    response = client.get("/traced-request")
    assert response.status_code == 200

    request = delivered.get(timeout=1)
    assert request.full_url.endswith("/api/v1/spans")

    payload = json.loads(request.data)
    assert payload["trace_id"]
    spans = payload["spans"]
    assert len(spans) == 3

    roots = [s for s in spans if s["parent_span_id"] is None]
    assert len(roots) == 1
    root = roots[0]
    assert root["kind"] == "controller"
    assert root["name"] == "GET traced-request"
    assert root["environment"] == "production"
    assert root["release"] == "v2.41.0"

    service_span = next(s for s in spans if s["kind"] == "service")
    assert service_span["name"] == "PaymentService.charge"
    assert service_span["parent_span_id"] == root["span_id"]

    query_span = next(s for s in spans if s["kind"] == "database")
    assert query_span["name"] == "SELECT"
    assert query_span["parent_span_id"] == service_span["span_id"]

    # Every span_id is unique within the trace, and every non-root parent_span_id resolves to
    # another span_id present in the same array: the server-side contract this client must honor.
    span_ids = [s["span_id"] for s in spans]
    assert len(span_ids) == len(set(span_ids))
    for s in spans:
        if s["parent_span_id"] is not None:
            assert s["parent_span_id"] in span_ids


def test_a_fast_request_never_sends_a_trace(monkeypatch):
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

    client = DjangoTestClient()
    response = client.get("/ok")
    assert response.status_code == 200

    assert delivered.empty()


def test_no_trace_is_sent_when_track_tracing_is_off_even_for_a_request_over_threshold(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)

    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        trace_capture_threshold_ms=0,
        track_tracing=False,
    )

    client = DjangoTestClient()
    response = client.get("/ok")
    assert response.status_code == 200

    assert delivered.empty()


@pytest.mark.django_db
def test_does_not_time_a_query_run_outside_any_request(monkeypatch):
    # connection.execute_wrapper is entered and exited fresh on every request (see
    # ForgeOpsTrackerPerformanceTrackingMiddleware's own comment): a query run with no request in
    # flight at all (a management command, here simulated directly) must never be recorded by it.
    from django.db import connection

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

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")

    forge_ops_tracker._performance_state()[1].flush()
    assert delivered.empty()


# Trace context: every request's trace id, the matched route on its errors, and W3C traceparent
# continuation in both directions.

INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING_PARENT_ID = "00f067aa0ba902b7"
INCOMING_TRACEPARENT = f"00-{INCOMING_TRACE_ID}-{INCOMING_PARENT_ID}-01"


def _init_capturing_deliveries(monkeypatch, **overrides):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        **overrides,
    )
    return delivered


def _next_delivery_to(delivered, path_suffix):
    """The next delivered payload to an endpoint ending in path_suffix, skipping any other kind
    (a session checkin, a performance sample) the same request happened to trigger."""
    while True:
        request = delivered.get(timeout=1)
        if request.full_url.endswith(path_suffix):
            return json.loads(request.data)


def test_an_unhandled_error_carries_the_route_and_a_fresh_w3c_trace_id(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch)

    DjangoTestClient(raise_request_exception=False).get("/throw")

    payload = _next_delivery_to(delivered, "/events")
    assert payload["transaction_name"] == "GET throw"
    assert payload["endpoint"] == "GET /throw"
    assert len(payload["trace_id"]) == 32
    assert payload["trace_id"] == payload["trace_id"].lower()
    assert payload["trace_id"] != "0" * 32


def test_an_unhandled_error_continues_an_incoming_traceparent(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch)

    DjangoTestClient(raise_request_exception=False).get("/throw", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)

    assert _next_delivery_to(delivered, "/events")["trace_id"] == INCOMING_TRACE_ID


def test_a_malformed_incoming_traceparent_starts_a_fresh_trace(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch)

    DjangoTestClient(raise_request_exception=False).get("/throw", HTTP_TRACEPARENT=INCOMING_TRACEPARENT.upper())

    payload = _next_delivery_to(delivered, "/events")
    assert payload["trace_id"] != INCOMING_TRACE_ID.upper()
    assert payload["trace_id"] != INCOMING_TRACE_ID
    assert len(payload["trace_id"]) == 32


def test_an_error_reported_from_inside_the_view_already_carries_the_route_pattern_not_the_path(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch)

    response = DjangoTestClient().get("/orders/7/handled", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)
    assert response.status_code == 200

    payload = _next_delivery_to(delivered, "/events")
    assert payload["transaction_name"] == "GET orders/<int:order_id>/handled"
    assert payload["endpoint"] == "GET /orders/<int:order_id>/handled"
    assert payload["trace_id"] == INCOMING_TRACE_ID
    assert "7" not in payload["endpoint"]


def test_every_request_gets_a_trace_id_even_with_track_tracing_off(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch, track_tracing=False)

    DjangoTestClient(raise_request_exception=False).get("/throw", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)

    assert _next_delivery_to(delivered, "/events")["trace_id"] == INCOMING_TRACE_ID


def test_trace_fields_are_never_pii_scrubbed(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch, scrub_pii=True)

    DjangoTestClient().get("/orders/7/handled")

    payload = _next_delivery_to(delivered, "/events")
    assert payload["endpoint"] == "GET /orders/<int:order_id>/handled"
    assert len(payload["trace_id"]) == 32


def test_a_fast_request_that_errored_still_sends_its_trace_with_the_root_under_the_remote_parent(monkeypatch):
    # The default 1000ms threshold: this request is fast, so only the error makes it worth sending.
    delivered = _init_capturing_deliveries(monkeypatch)

    DjangoTestClient(raise_request_exception=False).get("/throw", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)

    trace = _next_delivery_to(delivered, "/spans")
    assert trace["trace_id"] == INCOMING_TRACE_ID
    root = next(s for s in trace["spans"] if s["kind"] == "controller")
    assert root["name"] == "GET throw"
    assert root["parent_span_id"] == INCOMING_PARENT_ID


def test_a_fast_request_with_a_handled_error_still_sends_its_trace(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch)

    DjangoTestClient().get("/orders/7/handled")

    # The error event and the trace are sent by two separate worker threads, so either can arrive
    # first. Both are read before either is looked at: _next_delivery_to throws away whatever it
    # skips, which here would be the trace whenever the trace won.
    requests = [delivered.get(timeout=1), delivered.get(timeout=1)]
    error = next(json.loads(r.data) for r in requests if r.full_url.endswith("/events"))
    trace = next(json.loads(r.data) for r in requests if r.full_url.endswith("/spans"))
    assert trace["trace_id"] == error["trace_id"]


def test_a_slow_request_continuing_a_trace_nests_its_root_under_the_callers_span(monkeypatch):
    delivered = _init_capturing_deliveries(monkeypatch, trace_capture_threshold_ms=0)

    DjangoTestClient().get("/ok", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)

    trace = _next_delivery_to(delivered, "/spans")
    assert trace["trace_id"] == INCOMING_TRACE_ID
    assert trace["spans"][0]["parent_span_id"] == INCOMING_PARENT_ID


def test_an_outbound_call_during_the_request_carries_this_requests_traceparent(monkeypatch):
    import requests.adapters

    from forge_ops_tracker.integrations.requests import (
        _reset_for_testing,
        init_requests,
    )

    delivered = _init_capturing_deliveries(monkeypatch, trace_capture_threshold_ms=0)
    sent = []

    def fake_adapter_send(self, request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_adapter_send)
    init_requests()
    try:
        DjangoTestClient().get("/call-downstream", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)
    finally:
        _reset_for_testing()

    version, trace_id, span_id, flags = sent[0].headers["traceparent"].split("-")
    assert (version, trace_id, flags) == ("00", INCOMING_TRACE_ID, "01")

    trace = _next_delivery_to(delivered, "/spans")
    http_span = next(s for s in trace["spans"] if s["kind"] == "http")
    assert http_span["span_id"] == span_id
    assert http_span["name"] == "GET orders.internal.example"


def test_no_trace_state_is_left_behind_after_the_request(monkeypatch):
    _init_capturing_deliveries(monkeypatch)

    DjangoTestClient().get("/ok", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)

    assert forge_ops_tracker._request_state.get() is None


def test_nothing_is_traced_when_the_client_is_not_enabled(monkeypatch):
    delivered = queue.Queue()
    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", lambda request, timeout=None: delivered.put(request))
    forge_ops_tracker.init(dsn=None)

    DjangoTestClient(raise_request_exception=False).get("/throw", HTTP_TRACEPARENT=INCOMING_TRACEPARENT)

    assert delivered.empty()


# Masked SQL on database spans, and the opt-in EXPLAIN on PostgreSQL


@pytest.mark.django_db
def test_query_span_carries_the_masked_statement_and_db_system(monkeypatch):
    from django.db import connection
    from django.http import HttpResponse
    from django.test import RequestFactory

    from forge_ops_tracker.integrations.django import (
        ForgeOpsTrackerPerformanceTrackingMiddleware,
    )

    _init_capturing_deliveries(monkeypatch)
    seen = []

    def get_response(request):
        with connection.cursor() as cursor:
            cursor.execute("SELECT %s, 'secret@example.com'", [42])
        seen.append(forge_ops_tracker._spans.get())
        return HttpResponse("ok")

    token = forge_ops_tracker._start_span_trace()
    ForgeOpsTrackerPerformanceTrackingMiddleware(get_response)(RequestFactory().get("/some-path"))
    forge_ops_tracker._end_span_trace(token)

    span = seen[0].spans[0]
    assert span["kind"] == "database"
    assert span["data"] == {"db.statement": "SELECT %s, ?", "db.system": "sqlite"}
    assert "secret@example.com" not in json.dumps(seen[0].spans)


class _FakeCursor:
    def __init__(self, db_connection):
        self._db_connection = db_connection

    def execute(self, sql, params=None):
        self._db_connection.log.append(("execute", sql, params))
        if self._db_connection.fail_on and self._db_connection.fail_on in sql:
            raise RuntimeError("permission denied for table users")

    def fetchone(self):
        return [[{"Plan": {"Node Type": "Seq Scan"}}]]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeDjangoConnection:
    def __init__(self, alias, fail_on=None):
        self.alias = alias
        self.vendor = "postgresql"
        self.fail_on = fail_on
        self.log = []

    def set_autocommit(self, value):
        self.log.append(("set_autocommit", value))

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.log.append(("rollback",))

    def close(self):
        self.log.append(("close",))


class _FakeConnectionHandler:
    def __init__(self, fail_on=None):
        self.created = []
        self._fail_on = fail_on

    def create_connection(self, alias):
        db_connection = _FakeDjangoConnection(alias, self._fail_on)
        self.created.append(db_connection)
        return db_connection

    def __getitem__(self, alias):
        raise AssertionError("the EXPLAIN must never reuse a thread's existing connection")


def test_explain_opens_a_new_connection_in_a_read_only_transaction_and_rolls_back(monkeypatch):
    from forge_ops_tracker.integrations.django import _explain_on_new_connection

    handler = _FakeConnectionHandler()
    monkeypatch.setattr("django.db.connections", handler)

    plan = _explain_on_new_connection("default", "SELECT * FROM users WHERE email = %s", ["a@b.co"])

    assert plan == [{"Plan": {"Node Type": "Seq Scan"}}]
    assert [c.alias for c in handler.created] == ["default"]
    assert handler.created[0].log == [
        ("set_autocommit", False),
        ("execute", "SET TRANSACTION READ ONLY", None),
        ("execute", "SET LOCAL statement_timeout = '2s'", None),
        ("execute", "EXPLAIN (FORMAT JSON) SELECT * FROM users WHERE email = %s", ["a@b.co"]),
        ("rollback",),
        ("close",),
    ]


def test_explain_closes_its_connection_even_when_the_explain_fails(monkeypatch):
    from forge_ops_tracker.integrations.django import _explain_on_new_connection

    handler = _FakeConnectionHandler(fail_on="EXPLAIN")
    monkeypatch.setattr("django.db.connections", handler)

    with pytest.raises(RuntimeError):
        _explain_on_new_connection("default", "SELECT 1", None)
    assert handler.created[0].log[-1] == ("close",)


def test_slow_postgres_query_is_offered_to_the_planner_with_an_explain_on_a_new_connection(monkeypatch):
    from forge_ops_tracker.integrations import django as django_integration

    _init_capturing_deliveries(monkeypatch, explain_slow_queries=True, explain_threshold_ms=0)
    offered = []

    class _Planner:
        def consider(self, statement, db_system, duration_ms, explain, masked=None):
            offered.append((statement, db_system, explain))
            return True

    monkeypatch.setattr(forge_ops_tracker, "_query_planner_state", lambda: (None, _Planner()))
    explained = []
    monkeypatch.setattr(django_integration, "_explain_on_new_connection", lambda *args: explained.append(args))
    request_connection = _FakeDjangoConnection("replica")
    ran = []

    def execute(sql, params, many, context):
        ran.append(sql)

    middleware = django_integration.ForgeOpsTrackerPerformanceTrackingMiddleware(lambda request: None)
    middleware._record_query(execute, "SELECT * FROM users WHERE email = %s", ["a@b.co"], False, {"connection": request_connection})

    assert ran == ["SELECT * FROM users WHERE email = %s"]
    statement, db_system, explain = offered[0]
    assert (statement, db_system) == ("SELECT * FROM users WHERE email = %s", "postgresql")
    assert explained == []  # nothing runs on the request's own thread
    explain()
    assert explained == [("replica", "SELECT * FROM users WHERE email = %s", ["a@b.co"])]
    assert request_connection.log == []


def test_executemany_is_never_offered_to_the_planner(monkeypatch):
    from forge_ops_tracker.integrations import django as django_integration

    _init_capturing_deliveries(monkeypatch, explain_slow_queries=True, explain_threshold_ms=0)
    offered = []

    class _Planner:
        def consider(self, *args, **kwargs):
            offered.append(args)

    monkeypatch.setattr(forge_ops_tracker, "_query_planner_state", lambda: (None, _Planner()))
    middleware = django_integration.ForgeOpsTrackerPerformanceTrackingMiddleware(lambda request: None)
    middleware._record_query(lambda *args: None, "SELECT %s", [[1], [2]], True, {"connection": _FakeDjangoConnection("default")})

    assert offered == []
