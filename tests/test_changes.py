import json
import queue
import time
import urllib.error
from datetime import datetime, timezone

import pytest

import forge_ops_tracker
from forge_ops_tracker import changes
from forge_ops_tracker.configuration import Configuration

DSN = "https://secret-key@tracker.example.com/api/v1/events"


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _capture_requests(monkeypatch, respond=None):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        if respond is not None:
            return respond(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    return delivered


def _configuration(**overrides):
    config = Configuration()
    config.dsn = DSN
    config.environment = "production"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# record_change


def test_record_change_posts_the_full_payload_to_the_changes_endpoint(monkeypatch):
    delivered = _capture_requests(monkeypatch)
    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False)

    result = forge_ops_tracker.record_change(
        "feature_flag",
        "Enabled new_checkout for 10%",
        details={"flag": "new_checkout", "rollout": 10},
        service="web",
        actor="luke",
        url="https://flags.example.com/new_checkout",
        id="flag-42",
        occurred_at=datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert result is True
    request = delivered.get(timeout=2)
    assert request.full_url == "https://tracker.example.com/api/v1/changes"
    assert request.get_header("Authorization") == "Bearer secret-key"
    assert json.loads(request.data) == {
        "kind": "feature_flag",
        "title": "Enabled new_checkout for 10%",
        "details": {"flag": "new_checkout", "rollout": 10},
        "environment": "production",
        "service": "web",
        "actor": "luke",
        "url": "https://flags.example.com/new_checkout",
        "id": "flag-42",
        "occurred_at": "2026-09-25T12:00:00.000Z",
    }


def test_record_change_leaves_out_optional_fields_and_defaults_occurred_at_to_now():
    payload = changes.build_change(_configuration(), "config", "Raised the timeout")

    assert set(payload) == {"kind", "title", "details", "environment", "occurred_at"}
    assert payload["details"] == {}
    assert payload["environment"] == "production"
    occurred_at = datetime.fromisoformat(payload["occurred_at"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - occurred_at).total_seconds()) < 5


def test_an_explicit_environment_overrides_the_configured_one():
    assert changes.build_change(_configuration(), "config", "x", environment="staging")["environment"] == "staging"


@pytest.mark.parametrize("kind", ["feature_flag", "config", "migration", "dependency", "infrastructure", "other"])
def test_every_known_kind_is_sent_as_is(kind):
    assert changes.build_change(_configuration(), kind, "x")["kind"] == kind


@pytest.mark.parametrize("kind", ["deploy", "", None, "FEATURE_FLAG"])
def test_an_unknown_kind_is_sent_as_other(kind):
    assert changes.build_change(_configuration(), kind, "x")["kind"] == "other"


def test_title_is_truncated_to_200_characters_and_non_dict_details_are_dropped():
    payload = changes.build_change(_configuration(), "config", "x" * 300, details="not a dict")

    assert payload["title"] == "x" * 200
    assert payload["details"] == {}


def test_a_blank_title_sends_nothing(monkeypatch):
    delivered = _capture_requests(monkeypatch)
    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False)

    assert forge_ops_tracker.record_change("config", "   ") is False
    time.sleep(0.05)
    assert delivered.empty()


def test_record_change_is_a_no_op_when_not_enabled(monkeypatch):
    delivered = _capture_requests(monkeypatch)
    forge_ops_tracker.init(dsn=DSN, environment="development", install_excepthook=False)

    assert forge_ops_tracker.record_change("config", "Raised the timeout") is False
    time.sleep(0.05)
    assert delivered.empty()


@pytest.mark.parametrize("status", [403, 422, 500])
def test_record_change_never_raises_on_an_error_response(monkeypatch, status):
    def respond(request):
        raise urllib.error.HTTPError(request.full_url, status, "nope", {}, None)

    delivered = _capture_requests(monkeypatch, respond)
    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False)

    assert forge_ops_tracker.record_change("config", "x") is True
    delivered.get(timeout=2)


def test_record_change_never_raises_even_when_building_the_payload_fails(monkeypatch):
    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(changes, "build_change", boom)

    assert forge_ops_tracker.record_change("config", "x") is False


# Startup snapshot


def test_snapshot_payload_has_runtime_and_dependencies_but_no_env_var_names_by_default():
    payload = changes.build_snapshot(_configuration())

    assert payload["environment"] == "production"
    assert payload["state"]["runtime"].startswith("python 3.")
    dependencies = payload["state"]["dependencies"]
    assert dependencies["pytest"]
    assert all(isinstance(name, str) and isinstance(version, str) for name, version in dependencies.items())
    assert "env_var_names" not in payload["state"]
    assert "schema_version" not in payload["state"]


def test_snapshot_sends_env_var_names_never_values_when_opted_in(monkeypatch):
    monkeypatch.setattr(changes.os, "environ", {"DATABASE_URL": "postgres://secret", "HOSTNAME": "web-1", "STRIPE_KEY": "sk_live"})

    payload = changes.build_snapshot(_configuration(track_env_var_names=True))

    assert payload["state"]["env_var_names"] == ["DATABASE_URL", "STRIPE_KEY"]
    assert "secret" not in json.dumps(payload["state"]["env_var_names"])
    assert "sk_live" not in json.dumps(payload["state"]["env_var_names"])


def test_env_var_denylist_drops_host_specific_noise_and_forge_ops_settings():
    names = [
        "HOSTNAME", "HOST", "HOME", "PATH", "PWD", "OLDPWD", "SHLVL", "_", "TERM", "USER", "LOGNAME", "SHELL",
        "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "PORT", "DYNO", "INVOCATION_ID", "JOURNAL_STREAM",
        "SYSTEMD_EXEC_PID", "MEMORY_PRESSURE_WATCH", "KUBERNETES_SERVICE_HOST", "REDIS_SERVICE_HOST",
        "REDIS_SERVICE_PORT", "REDIS_SERVICE_PORT_HTTP", "REDIS_PORT_6379_TCP", "REDIS_PORT_6379_TCP_ADDR",
        "FORGE_OPS_DSN", "FORGE_OPS_RELEASE", "SECRET_KEY", "DJANGO_SETTINGS_MODULE", "DATABASE_URL",
    ]

    assert changes.env_var_names(dict.fromkeys(names, "value")) == ["DATABASE_URL", "DJANGO_SETTINGS_MODULE", "SECRET_KEY"]


def test_dependencies_are_capped_and_truncated(monkeypatch):
    class _Distribution:
        def __init__(self, name, version):
            self.metadata = {"Name": name}
            self.version = version

    fakes = [_Distribution(f"pkg-{i:05d}", "1.0") for i in range(3500)] + [_Distribution("z" * 500, "9" * 500)]
    monkeypatch.setattr("importlib.metadata.distributions", lambda: fakes)

    dependencies = changes.dependencies()

    assert len(dependencies) == 3000
    assert next(iter(dependencies)) == "pkg-00000"

    monkeypatch.setattr("importlib.metadata.distributions", lambda: fakes[-1:])
    ((name, version),) = changes.dependencies().items()
    assert len(name) == 200
    assert len(version) == 100


def test_dependencies_are_left_out_rather_than_guessed_when_metadata_fails(monkeypatch):
    def boom():
        raise RuntimeError("broken install")

    monkeypatch.setattr("importlib.metadata.distributions", boom)

    assert "dependencies" not in changes.build_snapshot(_configuration())["state"]


@pytest.fixture
def real_snapshot(monkeypatch):
    """conftest.py stubs _start_change_snapshot out for every other test; put the real one back."""
    monkeypatch.setattr(forge_ops_tracker, "_start_change_snapshot", _real_start_change_snapshot)


_real_start_change_snapshot = forge_ops_tracker._start_change_snapshot


def test_init_sends_one_snapshot_to_the_change_snapshots_endpoint(monkeypatch, real_snapshot):
    delivered = _capture_requests(monkeypatch)

    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False)
    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False)

    request = delivered.get(timeout=2)
    assert request.full_url == "https://tracker.example.com/api/v1/change_snapshots"
    body = json.loads(request.data)
    assert body["environment"] == "production"
    assert body["state"]["runtime"].startswith("python ")
    time.sleep(0.1)
    assert delivered.empty()  # once per process, not once per init()


def test_detect_changes_false_sends_no_snapshot(monkeypatch, real_snapshot):
    delivered = _capture_requests(monkeypatch)

    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False, detect_changes=False)

    time.sleep(0.1)
    assert delivered.empty()


def test_no_snapshot_when_not_enabled(monkeypatch, real_snapshot):
    delivered = _capture_requests(monkeypatch)

    forge_ops_tracker.init(dsn=DSN, environment="development", install_excepthook=False)

    time.sleep(0.1)
    assert delivered.empty()


def test_a_failing_snapshot_never_raises(monkeypatch, real_snapshot):
    def respond(request):
        raise urllib.error.URLError("connection refused")

    delivered = _capture_requests(monkeypatch, respond)

    forge_ops_tracker.init(dsn=DSN, environment="production", install_excepthook=False)

    delivered.get(timeout=2)
