"""The environment default and the one-time "not sending" warning, including end-to-end runs of a
fresh interpreter against a local HTTP server, the way a new customer's script would run."""

import http.server
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading

import pytest

import forge_ops_tracker
from forge_ops_tracker.configuration import Configuration

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
_DSN = "https://key@tracker.example.com/api/v1/events"


def test_environment_defaults_to_production_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("FORGE_OPS_ENVIRONMENT", raising=False)

    assert Configuration().environment == "production"


def test_environment_reads_forge_ops_environment(monkeypatch):
    monkeypatch.setenv("FORGE_OPS_ENVIRONMENT", "development")

    assert Configuration().environment == "development"


def test_an_empty_forge_ops_environment_falls_back_to_production(monkeypatch):
    monkeypatch.setenv("FORGE_OPS_ENVIRONMENT", "")

    assert Configuration().environment == "production"


def test_enabled_environments_default_is_unchanged():
    assert Configuration().enabled_environments == {"production", "staging"}


def test_a_dsn_alone_is_enough_to_be_enabled(monkeypatch):
    monkeypatch.delenv("FORGE_OPS_ENVIRONMENT", raising=False)

    assert forge_ops_tracker.init(dsn=_DSN).is_enabled() is True


def test_init_warns_once_when_the_environment_is_not_enabled(caplog):
    with caplog.at_level(logging.WARNING, logger="forge_ops_tracker"):
        forge_ops_tracker.init(dsn=_DSN, environment="development")
        forge_ops_tracker.init(dsn=_DSN, environment="development")

    warnings = [r.getMessage() for r in caplog.records if r.name == "forge_ops_tracker"]
    expected = (
        '[ForgeOps] Not sending: this environment is "development", and only production, staging are '
        'enabled. Set FORGE_OPS_ENVIRONMENT=production (or add "development" to enabled_environments) '
        "to send from here."
    )
    assert warnings == [expected]


def test_init_does_not_warn_without_a_dsn(caplog, monkeypatch):
    monkeypatch.delenv("FORGE_OPS_DSN", raising=False)
    with caplog.at_level(logging.WARNING, logger="forge_ops_tracker"):
        forge_ops_tracker.init(environment="development")

    assert [r for r in caplog.records if r.name == "forge_ops_tracker"] == []


def test_init_does_not_warn_in_an_enabled_environment(caplog):
    with caplog.at_level(logging.WARNING, logger="forge_ops_tracker"):
        forge_ops_tracker.init(dsn=_DSN, environment="staging")

    assert [r for r in caplog.records if r.name == "forge_ops_tracker"] == []


def test_init_does_not_warn_when_the_environment_was_added_to_enabled_environments(caplog):
    with caplog.at_level(logging.WARNING, logger="forge_ops_tracker"):
        forge_ops_tracker.init(dsn=_DSN, environment="development", enabled_environments={"development"})

    assert [r for r in caplog.records if r.name == "forge_ops_tracker"] == []


class _Collector(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.received.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(202)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def collector():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _run_child(collector, extra_env=None):
    script = textwrap.dedent(
        """
        import forge_ops_tracker
        forge_ops_tracker.init()
        raise RuntimeError("first error from a new customer")
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_OPS_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [_SRC, os.environ.get("PYTHONPATH")]))
    env["FORGE_OPS_DSN"] = f"http://key@127.0.0.1:{collector.server_address[1]}/api/v1/events"
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)


def _events(collector):
    return [json.loads(body) for path, _auth, body in collector.received if path == "/api/v1/events"]


def test_a_child_process_with_only_a_dsn_set_delivers_its_error(collector):
    result = _run_child(collector)

    assert result.returncode == 1
    assert "[ForgeOps]" not in result.stderr
    events = _events(collector)
    assert [e["message"] for e in events] == ["first error from a new customer"]
    assert events[0]["environment"] == "production"


def test_a_child_process_in_development_sends_nothing_and_warns_once(collector):
    result = _run_child(collector, {"FORGE_OPS_ENVIRONMENT": "development"})

    assert result.returncode == 1
    assert "first error from a new customer" in result.stderr  # the normal traceback still prints
    assert result.stderr.count("[ForgeOps] Not sending") == 1
    assert 'this environment is "development"' in result.stderr
    assert collector.received == []
