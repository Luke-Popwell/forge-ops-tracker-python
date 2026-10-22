import queue
import sys

import pytest

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


def test_init_configures_and_returns_the_configuration():
    config = forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        release="abc123",
    )

    assert config.dsn == "https://key@tracker.example.com/api/v1/events"
    assert config.release == "abc123"


def test_init_rejects_an_unknown_configuration_attribute():
    with pytest.raises(TypeError):
        forge_ops_tracker.init(not_a_real_setting=True)


def test_capture_exception_delivers_through_the_full_stack(monkeypatch):
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
    assert request.get_header("Authorization") == "Bearer key"


def test_capture_exception_defaults_to_the_currently_handled_exception(monkeypatch):
    delivered = _patch_urlopen_capturing(monkeypatch)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    try:
        raise ValueError("boom")
    except ValueError:
        forge_ops_tracker.capture_exception()  # no args: picks up sys.exc_info()

    request = delivered.get(timeout=1)
    assert request.get_header("Authorization") == "Bearer key"


def test_capture_exception_is_a_no_op_with_nothing_currently_being_handled():
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events")

    forge_ops_tracker.capture_exception()  # must not raise


def test_excepthook_reports_then_still_calls_the_original_hook(monkeypatch):
    delivered = _patch_urlopen_capturing(monkeypatch)
    # Install a known fake as sys.excepthook *before* init(), the same way
    # some other library's hook might already be installed in a real app:
    # init()'s own wrapper should capture and still call this one.
    original_hook_calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: original_hook_calls.append(args))

    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    try:
        raise ValueError("uncaught")
    except ValueError:
        exc_type, exc_value, exc_tb = sys.exc_info()

    sys.excepthook(exc_type, exc_value, exc_tb)  # this is now forge_ops_tracker's wrapper

    assert len(original_hook_calls) == 1
    request = delivered.get(timeout=1)
    assert request.get_header("Authorization") == "Bearer key"


def test_set_user_attaches_the_user_to_a_later_capture_exception_call(monkeypatch):
    delivered = _patch_urlopen_capturing(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "forge_ops_tracker.EventBuilder.build",
        lambda self, exc, context=None, user=None, breadcrumbs=None, **_trace_fields: captured.update(user=user) or {"exception_class": "ValueError"},
    )
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    forge_ops_tracker.set_user(id=42, email="alice@example.com")
    try:
        raise ValueError("boom")
    except ValueError as error:
        forge_ops_tracker.capture_exception(error)

    delivered.get(timeout=1)
    assert captured["user"] == {"id": 42, "email": "alice@example.com"}


def test_an_explicit_user_argument_overrides_whatever_set_user_last_set(monkeypatch):
    delivered = _patch_urlopen_capturing(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "forge_ops_tracker.EventBuilder.build",
        lambda self, exc, context=None, user=None, breadcrumbs=None, **_trace_fields: captured.update(user=user) or {"exception_class": "ValueError"},
    )
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    forge_ops_tracker.set_user(id=42)
    try:
        raise ValueError("boom")
    except ValueError as error:
        forge_ops_tracker.capture_exception(error, user={"id": 99})

    delivered.get(timeout=1)
    assert captured["user"] == {"id": 99}


def test_set_user_with_nothing_clears_whatever_was_set():
    forge_ops_tracker.set_user(id=42)

    forge_ops_tracker.set_user()

    assert forge_ops_tracker._local.user is None


def test_set_user_omits_fields_left_as_none():
    forge_ops_tracker.set_user(id=42)

    assert forge_ops_tracker._local.user == {"id": 42}


def test_install_excepthook_false_leaves_sys_excepthook_untouched():
    original = sys.excepthook

    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        install_excepthook=False,
    )

    assert sys.excepthook is original


def test_capture_exception_outside_a_request_sends_no_trace_fields(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "forge_ops_tracker.EventBuilder.build",
        lambda self, exc, **kwargs: captured.update(kwargs) or {"exception_class": "ValueError"},
    )
    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", lambda request, timeout=None: None)
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events", enabled_environments={"production"}, environment="production")

    forge_ops_tracker.capture_exception(ValueError("boom"))

    assert captured["transaction_name"] is None
    assert captured["endpoint"] is None
    assert captured["trace_id"] is None
