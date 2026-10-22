import json
import queue

import forge_ops_tracker


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patch_urlopen_capturing(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    return delivered


def test_add_breadcrumb_lazily_creates_a_buffer_with_no_trail_started_at_all():
    # No _start_breadcrumb_trail() call anywhere here: the manual API must still work standalone,
    # the same "works outside a request entirely" contract set_user() already has.
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events")

    forge_ops_tracker.add_breadcrumb("charged card", category="billing", data={"order_id": 42})

    buffer = forge_ops_tracker._breadcrumbs.get()
    assert buffer is not None
    entry = buffer.all()[0]
    assert entry["category"] == "billing"
    assert entry["message"] == "charged card"
    assert entry["data"] == {"order_id": 42}


def test_add_breadcrumb_defaults_category_to_custom_and_level_to_info():
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events")

    forge_ops_tracker.add_breadcrumb("checkpoint")

    entry = forge_ops_tracker._breadcrumbs.get().all()[0]
    assert entry["category"] == "custom"
    assert entry["level"] == "info"
    assert entry["data"] == {}


def test_start_and_end_breadcrumb_trail_give_two_sequential_requests_on_the_same_context_isolated_trails():
    # Simulates two WSGI requests handled back to back on the same worker thread: the second
    # request's trail must never see the first's entries, the exact contract
    # ForgeOpsTracker::Middleware::BreadcrumbContext's own spec establishes for Ruby.
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    token_one = forge_ops_tracker._start_breadcrumb_trail()
    forge_ops_tracker.add_breadcrumb("first request breadcrumb")
    first_buffer = forge_ops_tracker._breadcrumbs.get()
    forge_ops_tracker._end_breadcrumb_trail(token_one)

    assert forge_ops_tracker._breadcrumbs.get() is None

    token_two = forge_ops_tracker._start_breadcrumb_trail()
    second_buffer = forge_ops_tracker._breadcrumbs.get()

    assert second_buffer is not first_buffer
    assert second_buffer.all() == []
    assert first_buffer.all() == [
        {
            "category": "custom",
            "message": "first request breadcrumb",
            "level": "info",
            "timestamp": first_buffer.all()[0]["timestamp"],
            "data": {},
        }
    ]
    forge_ops_tracker._end_breadcrumb_trail(token_two)


def test_end_breadcrumb_trail_clears_even_when_the_caller_raises():
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    token = forge_ops_tracker._start_breadcrumb_trail()
    assert forge_ops_tracker._breadcrumbs.get() is not None  # confirms a real trail actually started
    try:
        try:
            raise ValueError("boom")
        finally:
            forge_ops_tracker._end_breadcrumb_trail(token)
    except ValueError:
        pass

    assert forge_ops_tracker._breadcrumbs.get() is None


def test_start_breadcrumb_trail_is_a_no_op_when_track_breadcrumbs_is_off():
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events", track_breadcrumbs=False)

    token = forge_ops_tracker._start_breadcrumb_trail()

    assert token is None
    assert forge_ops_tracker._breadcrumbs.get() is None


def test_start_breadcrumb_trail_is_a_no_op_when_the_client_is_not_enabled():
    forge_ops_tracker.init(dsn=None)  # no DSN: not enabled

    token = forge_ops_tracker._start_breadcrumb_trail()

    assert token is None
    assert forge_ops_tracker._breadcrumbs.get() is None


def test_record_breadcrumb_no_ops_when_track_breadcrumbs_is_off_even_with_an_active_trail():
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events")
    forge_ops_tracker.add_breadcrumb("already here")  # lazily creates a buffer
    forge_ops_tracker._configuration.track_breadcrumbs = False

    forge_ops_tracker._record_breadcrumb("should not appear", category="query")

    assert [e["message"] for e in forge_ops_tracker._breadcrumbs.get().all()] == ["already here"]


def test_capture_exception_attaches_the_current_trail_to_the_reported_event(monkeypatch):
    delivered = _patch_urlopen_capturing(monkeypatch)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    token = forge_ops_tracker._start_breadcrumb_trail()
    forge_ops_tracker.add_breadcrumb("ran query", category="query", data={"duration_ms": 1.2})
    try:
        raise ValueError("boom")
    except ValueError as error:
        forge_ops_tracker.capture_exception(error)
    finally:
        forge_ops_tracker._end_breadcrumb_trail(token)

    request = delivered.get(timeout=1)
    payload = json.loads(request.data)
    assert payload["breadcrumbs"] == [
        {
            "category": "query",
            "message": "ran query",
            "level": "info",
            "timestamp": payload["breadcrumbs"][0]["timestamp"],
            "data": {"duration_ms": 1.2},
        }
    ]


def test_capture_exception_omits_breadcrumbs_entirely_with_no_trail_active(monkeypatch):
    delivered = _patch_urlopen_capturing(monkeypatch)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    try:
        raise ValueError("boom")
    except ValueError as error:
        forge_ops_tracker.capture_exception(error)

    request = delivered.get(timeout=1)
    payload = json.loads(request.data)
    assert "breadcrumbs" not in payload


def test_capture_exception_scrubs_breadcrumb_content_by_default(monkeypatch):
    delivered = _patch_urlopen_capturing(monkeypatch)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    token = forge_ops_tracker._start_breadcrumb_trail()
    forge_ops_tracker.add_breadcrumb("failed for user@example.com", category="custom", data={"password": "hunter2"})
    try:
        raise ValueError("boom")
    except ValueError as error:
        forge_ops_tracker.capture_exception(error)
    finally:
        forge_ops_tracker._end_breadcrumb_trail(token)

    request = delivered.get(timeout=1)
    payload = json.loads(request.data)
    breadcrumb = payload["breadcrumbs"][0]
    assert breadcrumb["message"] == "failed for [EMAIL FILTERED]"
    assert breadcrumb["data"] == {"password": "[FILTERED]"}
