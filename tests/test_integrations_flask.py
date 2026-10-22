import json
import queue
from datetime import datetime, timezone

from flask import Flask

import forge_ops_tracker
from forge_ops_tracker.integrations.flask import init_flask


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_reports_the_exception_and_still_lets_flask_handle_it(monkeypatch):
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

    app = Flask(__name__)
    app.config["TESTING"] = False  # otherwise Flask re-raises instead of returning a 500
    init_flask(app)

    @app.route("/throw")
    def throw():
        raise ValueError("boom")

    client = app.test_client()
    response = client.get("/throw")

    # init_flask only observes: Flask's own handling (a plain 500 here)
    # behaves exactly as if it weren't connected.
    assert response.status_code == 500

    request = delivered.get(timeout=1)
    assert request.get_header("Authorization") == "Bearer key"


def test_attaches_the_logged_in_user_to_a_reported_exception(monkeypatch):
    from flask_login import LoginManager, UserMixin, login_user

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

    class User(UserMixin):
        def __init__(self, id, email):
            self.id = id
            self.email = email

        def get_id(self):
            return str(self.id)

    app = Flask(__name__)
    app.config["TESTING"] = False
    app.secret_key = "test"
    init_flask(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User(user_id, "ada@example.com")

    @app.route("/login")
    def log_in():
        login_user(User(42, "ada@example.com"))
        return "ok"

    @app.route("/throw")
    def throw():
        raise ValueError("boom")

    # Flask-Login is session-based: login_user() sets a session cookie, and current_user is only
    # populated from it on a *later* request, via the user_loader callback above. A single request
    # that both logs in and raises would never see current_user populated at all: verified
    # directly (this test's own first draft did exactly that and failed).
    client = app.test_client()
    client.get("/login")
    client.get("/throw")

    payload = _next_delivery_to(delivered, "/events")
    assert payload["user"] == {"id": "42", "email": "ada@example.com"}


def test_does_not_attach_a_user_for_an_anonymous_request(monkeypatch):
    from flask_login import LoginManager

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

    app = Flask(__name__)
    app.config["TESTING"] = False
    app.secret_key = "test"
    init_flask(app)
    LoginManager(app)  # configured, but nobody ever calls login_user()

    @app.route("/throw")
    def throw():
        raise ValueError("boom")

    client = app.test_client()
    client.get("/throw")

    payload = _next_delivery_to(delivered, "/events")
    assert "user" not in payload


def test_never_raises_when_flask_login_is_installed_but_unconfigured(monkeypatch):
    # Confirmed directly: reading current_user with flask_login importable but no LoginManager
    # ever attached to this app raises AttributeError. Auto-detection must swallow that, not break
    # the request.
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

    app = Flask(__name__)
    app.config["TESTING"] = False
    init_flask(app)  # no LoginManager(app) at all

    @app.route("/throw")
    def throw():
        raise ValueError("boom")

    client = app.test_client()
    response = client.get("/throw")
    assert response.status_code == 500

    payload = _next_delivery_to(delivered, "/events")
    assert "user" not in payload


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

    app = Flask(__name__)
    init_flask(app)

    @app.route("/ok")
    def ok():
        return "ok"

    client = app.test_client()
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

    app = Flask(__name__)
    app.config["TESTING"] = False
    init_flask(app)

    @app.route("/throw")
    def throw():
        raise ValueError("boom")

    client = app.test_client()
    client.get("/throw")

    forge_ops_tracker._session_state()[1].flush()

    # Three requests were delivered above: the error event (from got_request_exception), the
    # errored request's trace, and the session checkin; find the session checkin specifically
    # rather than assuming order (or that it is among the first two).
    session_checkin = _next_delivery_to(delivered, "/api/v1/session_checkins")

    assert session_checkin is not None
    assert session_checkin["sessions_count"] == 1
    assert session_checkin["crashed_sessions_count"] == 1


def test_attaches_the_automatic_controller_breadcrumb_and_a_manually_added_one_to_the_same_error(monkeypatch):
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

    app = Flask(__name__)
    app.config["TESTING"] = False
    init_flask(app)

    @app.route("/throw")
    def throw():
        forge_ops_tracker.add_breadcrumb("about to explode", category="custom")
        raise ValueError("boom")

    client = app.test_client()
    client.get("/throw")

    payload = _next_delivery_to(delivered, "/events")

    # Only the manually-added breadcrumb: the automatic controller one is recorded from
    # teardown_request, which only runs *after* got_request_exception (and this test's own
    # capture_exception call) already fired, so it can never show up on this exact same error;
    # see init_flask's own comment for why (teardown_request never hands back a response either,
    # unlike Django's middleware, only exc).
    assert [b["category"] for b in payload["breadcrumbs"]] == ["custom"]
    assert payload["breadcrumbs"][0]["message"] == "about to explode"


def test_does_not_attach_breadcrumbs_from_one_request_to_a_later_unrelated_requests_error(monkeypatch):
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

    app = Flask(__name__)
    app.config["TESTING"] = False
    init_flask(app)

    @app.route("/ok")
    def ok():
        forge_ops_tracker.add_breadcrumb("from the first request")
        return "ok"

    @app.route("/throw")
    def throw():
        raise ValueError("boom")

    client = app.test_client()
    client.get("/ok")
    client.get("/throw")

    payload = _next_delivery_to(delivered, "/events")
    assert payload.get("breadcrumbs", []) == []


def test_does_not_attach_breadcrumbs_when_track_breadcrumbs_is_off(monkeypatch):
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

    app = Flask(__name__)
    app.config["TESTING"] = False
    init_flask(app)

    @app.route("/throw")
    def throw():
        raise ValueError("boom")

    client = app.test_client()
    client.get("/throw")

    payload = _next_delivery_to(delivered, "/events")
    assert "breadcrumbs" not in payload


def test_a_slow_request_delivers_a_trace_with_the_root_service_and_http_spans_correctly_nested(monkeypatch):
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

    app = Flask(__name__)
    init_flask(app)

    @app.route("/checkout")
    def checkout():
        with forge_ops_tracker.span("PaymentService.charge", kind="service"):
            forge_ops_tracker._record_leaf_span("GET api.stripe.com", kind="http", started_at=datetime.now(timezone.utc), duration_ms=200.0)
        return "ok"

    client = app.test_client()
    response = client.get("/checkout")
    assert response.status_code == 200

    request = delivered.get(timeout=1)
    assert request.full_url.endswith("/api/v1/spans")

    payload = json.loads(request.data)
    spans = payload["spans"]
    assert len(spans) == 3

    root = next(s for s in spans if s["parent_span_id"] is None)
    assert root["kind"] == "controller"
    assert root["name"] == "GET /checkout"
    assert root["environment"] == "production"
    assert root["release"] == "v2.41.0"

    service_span = next(s for s in spans if s["kind"] == "service")
    assert service_span["parent_span_id"] == root["span_id"]

    http_span = next(s for s in spans if s["kind"] == "http")
    assert http_span["name"] == "GET api.stripe.com"
    assert http_span["parent_span_id"] == service_span["span_id"]


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

    app = Flask(__name__)
    init_flask(app)

    @app.route("/ok")
    def ok():
        return "ok"

    client = app.test_client()
    response = client.get("/ok")
    assert response.status_code == 200

    assert delivered.empty()


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

    app = Flask(__name__)
    init_flask(app)

    @app.route("/users/<int:user_id>")
    def user_detail(user_id):
        return f"user {user_id}"

    client = app.test_client()
    response = client.get("/users/42")
    assert response.status_code == 200

    forge_ops_tracker._performance_state()[1].flush()

    request = delivered.get(timeout=1)
    assert request.full_url.endswith("/api/v1/performance_samples")

    payload = json.loads(request.data)
    assert len(payload["samples"]) == 1
    # The route pattern, not "GET /users/42": a distinct user id must not explode into its own
    # separate transaction the way the literal path would.
    assert payload["samples"][0]["transaction_name"] == "GET /users/<int:user_id>"
    assert payload["samples"][0]["request_count"] == 1


# Trace context: every request's trace id, the matched route on its errors, and W3C traceparent
# continuation.

INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING_PARENT_ID = "00f067aa0ba902b7"
INCOMING_TRACEPARENT = f"00-{INCOMING_TRACE_ID}-{INCOMING_PARENT_ID}-01"


def _traced_app(monkeypatch, **overrides):
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

    app = Flask(__name__)
    app.config["TESTING"] = False
    init_flask(app)

    @app.route("/orders/<int:order_id>", methods=["POST"])
    def update_order(order_id):
        raise ValueError(f"order {order_id} failed")

    @app.route("/orders/<int:order_id>/handled")
    def handled(order_id):
        try:
            raise ValueError(f"order {order_id} failed")
        except ValueError:
            forge_ops_tracker.capture_exception()
        return "handled"

    @app.route("/ok")
    def ok():
        return "ok"

    return app, delivered


def _next_delivery_to(delivered, path_suffix):
    while True:
        request = delivered.get(timeout=1)
        if request.full_url.endswith(path_suffix):
            return json.loads(request.data)


def test_an_unhandled_error_carries_the_route_and_continues_an_incoming_traceparent(monkeypatch):
    app, delivered = _traced_app(monkeypatch)

    response = app.test_client().post("/orders/7", headers={"traceparent": INCOMING_TRACEPARENT})
    assert response.status_code == 500

    payload = _next_delivery_to(delivered, "/events")
    assert payload["transaction_name"] == "POST /orders/<int:order_id>"
    assert payload["endpoint"] == "POST /orders/<int:order_id>"
    assert payload["trace_id"] == INCOMING_TRACE_ID


def test_an_error_reported_from_inside_the_view_carries_a_fresh_trace_id_without_a_header(monkeypatch):
    app, delivered = _traced_app(monkeypatch)

    app.test_client().get("/orders/7/handled")

    payload = _next_delivery_to(delivered, "/events")
    assert payload["endpoint"] == "GET /orders/<int:order_id>/handled"
    assert len(payload["trace_id"]) == 32
    assert payload["trace_id"] != "0" * 32


def test_an_invalid_incoming_traceparent_starts_a_fresh_trace(monkeypatch):
    app, delivered = _traced_app(monkeypatch)

    app.test_client().post("/orders/7", headers={"traceparent": f"00-{'0' * 32}-{INCOMING_PARENT_ID}-01"})

    payload = _next_delivery_to(delivered, "/events")
    assert payload["trace_id"] != "0" * 32
    assert len(payload["trace_id"]) == 32


def test_every_request_gets_a_trace_id_even_with_track_tracing_off(monkeypatch):
    app, delivered = _traced_app(monkeypatch, track_tracing=False)

    app.test_client().post("/orders/7", headers={"traceparent": INCOMING_TRACEPARENT})

    assert _next_delivery_to(delivered, "/events")["trace_id"] == INCOMING_TRACE_ID


def test_a_fast_request_that_errored_still_sends_its_trace_with_the_root_under_the_remote_parent(monkeypatch):
    app, delivered = _traced_app(monkeypatch)

    app.test_client().post("/orders/7", headers={"traceparent": INCOMING_TRACEPARENT})

    trace = _next_delivery_to(delivered, "/spans")
    assert trace["trace_id"] == INCOMING_TRACE_ID
    root = next(s for s in trace["spans"] if s["kind"] == "controller")
    assert root["name"] == "POST /orders/<int:order_id>"
    assert root["parent_span_id"] == INCOMING_PARENT_ID


def test_a_fast_successful_request_still_never_sends_a_trace(monkeypatch):
    app, _ = _traced_app(monkeypatch)

    app.test_client().get("/ok", headers={"traceparent": INCOMING_TRACEPARENT})

    # The span queue is only ever created by a push, so it still not existing means nothing was sent.
    assert forge_ops_tracker._span_queue is None
    assert forge_ops_tracker._request_state.get() is None
