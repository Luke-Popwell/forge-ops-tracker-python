import datetime
import hashlib
import json
import os
import queue

import pytest

import forge_ops_tracker
from forge_ops_tracker import strict_privacy
from forge_ops_tracker.breadcrumb_buffer import BreadcrumbBuffer
from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.event_builder import EventBuilder

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# A byte-identical copy of sdks/strict_privacy_vectors.json, generated from the server's own
# StrictPrivacy; every SDK checks itself against the same file.
with open(os.path.join(THIS_DIR, "fixtures", "strict_privacy_vectors.json"), encoding="utf-8") as vectors_file:
    VECTORS = json.load(vectors_file)


@pytest.mark.parametrize("vector", VECTORS["fingerprint"], ids=lambda vector: repr(vector["message"]))
def test_normalizes_and_fingerprints_every_vector_exactly_as_the_server_does(vector):
    assert strict_privacy.normalize_message(vector["message"]) == vector["normalized"]
    assert strict_privacy.message_fingerprint(vector["message"]) == vector["fingerprint"]
    assert vector["fingerprint"] == hashlib.sha256(vector["normalized"].encode("utf-8")).hexdigest()[:32]


def test_an_absent_message_fingerprints_as_an_empty_one():
    assert strict_privacy.message_fingerprint(None) == strict_privacy.message_fingerprint("")


@pytest.mark.parametrize("vector", VECTORS["span_name"], ids=lambda vector: vector["name"])
def test_names_every_span_vector_exactly_as_the_server_does(vector):
    assert strict_privacy.span_name(vector["name"]) == vector["strict"]


@pytest.mark.parametrize("vector", VECTORS["path"], ids=lambda vector: vector["path"] or "(empty)")
def test_reduces_every_path_vector_exactly_as_the_server_does(vector):
    assert strict_privacy.strict_path(vector["path"]) == vector["strict"]


def test_transaction_name_keeps_a_route_and_cuts_a_raw_path_down_to_its_first_segment():
    assert strict_privacy.transaction_name("GET /patients/<int:id>") == "GET /patients/<int:id>"
    assert strict_privacy.transaction_name("GET /patients/8821") == "GET /patients/:path"
    assert strict_privacy.transaction_name("CheckoutView") == "CheckoutView"
    assert strict_privacy.transaction_name(None) is None


def test_an_event_with_strict_privacy_on_cuts_down_a_raw_path_transaction_name():
    payload = strict_privacy.event({"message": "boom", "backtrace": [], "transaction_name": "GET /patients/8821"})

    assert payload["transaction_name"] == "GET /patients/:path"


@pytest.mark.parametrize("strict", [True, False])
def test_performance_samples_cut_down_raw_path_names_only_with_strict_privacy_on(strict):
    from unittest import mock

    from forge_ops_tracker.delivery_status import DELIVERED
    from forge_ops_tracker.performance_flusher import PerformanceFlusher

    config = Configuration()
    config.environment = "production"
    config.strict_privacy = strict
    delivered = []
    client = mock.Mock()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.extend(samples) or DELIVERED
    flusher = PerformanceFlusher(config, client)

    flusher.record("GET /patients/8821", 5.0)
    flusher.record("GET /patients/<int:id>", 5.0)
    flusher.flush()

    names = sorted(sample["transaction_name"] for sample in delivered)
    expected_raw = "GET /patients/:path" if strict else "GET /patients/8821"
    assert names == sorted([expected_raw, "GET /patients/<int:id>"])


def test_is_off_with_no_allowed_fields_by_default(monkeypatch):
    monkeypatch.delenv("FORGE_OPS_STRICT_PRIVACY", raising=False)
    monkeypatch.delenv("FORGE_OPS_PRIVACY_ALLOWED_FIELDS", raising=False)
    config = Configuration()

    assert config.strict_privacy is False
    assert config.privacy_allowed_fields == []


def test_reads_both_options_from_the_environment(monkeypatch):
    monkeypatch.setenv("FORGE_OPS_STRICT_PRIVACY", "true")
    monkeypatch.setenv("FORGE_OPS_PRIVACY_ALLOWED_FIELDS", "error_code, feature,")
    config = Configuration()

    assert config.strict_privacy is True
    assert config.privacy_allowed_fields == ["error_code", "feature"]


def test_an_explicit_init_option_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("FORGE_OPS_STRICT_PRIVACY", "true")
    monkeypatch.setenv("FORGE_OPS_PRIVACY_ALLOWED_FIELDS", "error_code")
    forge_ops_tracker._reset_for_testing()

    config = forge_ops_tracker.init(strict_privacy=False, privacy_allowed_fields=["feature"])

    assert config.strict_privacy is False
    assert config.privacy_allowed_fields == ["feature"]


def test_anything_but_a_true_like_value_is_off(monkeypatch):
    monkeypatch.setenv("FORGE_OPS_STRICT_PRIVACY", "false")

    assert Configuration().strict_privacy is False


class PatientLookupError(Exception):
    def __init__(self, message, statement):
        super().__init__(message)
        self.statement = statement


def _source_file(tmp_path):
    path = tmp_path / "app_file.py"
    path.write_text("\n".join(f"line {n}" for n in range(1, 21)))
    return path


def _strict_event_configuration(tmp_path):
    config = Configuration()
    config.environment = "production"
    config.release = "abc123"
    config.server_name = "web-1"
    config.app_root = str(tmp_path)
    config.capture_sql_statement = True
    return config


def _raise_from(path):
    # Runs the raise from a file under app_root, so its frame is in-app and would get source
    # context with strict privacy off.
    code = compile("\n" * 9 + "raise PatientLookupError(message, statement)\n", str(path), "exec")
    try:
        exec(code, {"PatientLookupError": PatientLookupError, "message": "Couldn't find Patient with id=8821 for jane@example.com", "statement": "SELECT * FROM patients WHERE id = 8821"})
    except PatientLookupError as e:
        return e


def _build(config, error):
    return EventBuilder(config).build(
        error,
        context={"error_code": "E42", "feature": {"nested": "no"}, "diagnosis": "flu", "tier": 3},
        user={"id": 7, "email": "jane@example.com", "username": "jane"},
        breadcrumbs=[{"category": "custom", "message": "viewed Jane's chart", "level": "info", "timestamp": "now", "data": {}}],
        transaction_name="GET /patients/<int:id>",
        endpoint="GET /patients/8821",
        trace_id="a" * 32,
    )


def test_an_event_with_strict_privacy_on_carries_only_structured_facts(tmp_path):
    config = _strict_event_configuration(tmp_path)
    config.strict_privacy = True
    config.privacy_allowed_fields = ["error_code", "feature", "tier"]
    path = _source_file(tmp_path)

    payload = _build(config, _raise_from(path))

    assert payload["message"] == ""
    # From the message as it would otherwise have been sent: after PII scrubbing.
    assert payload["message_fingerprint"] == strict_privacy.message_fingerprint("Couldn't find Patient with id=8821 for [EMAIL FILTERED]")
    for key in ("breadcrumbs", "endpoint", "sql_statement", "tags"):
        assert key not in payload
    assert payload["context"] == {"error_code": "E42", "tier": 3}
    assert payload["user"] == {"id": "7"}
    frame = next(frame for frame in payload["backtrace"] if frame["file"] == str(path))
    assert frame == {"file": str(path), "line": 10, "method": "<module>", "in_app": True}
    for frame in payload["backtrace"]:
        assert not {"context_line", "pre_context", "post_context"} & set(frame)
    assert payload["sql_objects"]["relations"] == ["patients"]
    assert payload["transaction_name"] == "GET /patients/<int:id>"
    assert payload["trace_id"] == "a" * 32
    assert payload["exception_class"].endswith("PatientLookupError")
    serialized = json.dumps(payload)
    for text in ("jane", "8821", "flu"):
        assert text not in serialized


def test_an_event_with_strict_privacy_on_and_no_allowed_fields_has_no_context_at_all(tmp_path):
    config = _strict_event_configuration(tmp_path)
    config.strict_privacy = True

    payload = _build(config, _raise_from(_source_file(tmp_path)))

    assert "context" not in payload
    assert "tags" not in payload


def test_an_event_with_strict_privacy_on_leaves_out_a_user_with_no_id(tmp_path):
    config = _strict_event_configuration(tmp_path)
    config.strict_privacy = True

    payload = EventBuilder(config).build(_raise_from(_source_file(tmp_path)), user={"email": "jane@example.com"})

    assert "user" not in payload


def test_an_event_with_strict_privacy_off_is_exactly_as_before(tmp_path):
    config = _strict_event_configuration(tmp_path)
    path = _source_file(tmp_path)

    payload = _build(config, _raise_from(path))

    assert payload["message"] == "Couldn't find Patient with id=8821 for [EMAIL FILTERED]"
    assert "message_fingerprint" not in payload
    assert payload["breadcrumbs"][0]["message"] == "viewed Jane's chart"
    assert payload["endpoint"] == "GET /patients/8821"
    assert payload["sql_statement"] == "SELECT * FROM patients WHERE id = ?"
    assert payload["context"] == {"error_code": "E42", "feature": {"nested": "no"}, "diagnosis": "flu", "tier": 3}
    assert payload["user"] == {"id": 7, "email": "jane@example.com", "username": "jane"}
    assert payload["tags"] == {}
    frame = next(frame for frame in payload["backtrace"] if frame["file"] == str(path))
    assert frame["context_line"] == "line 10"


def test_breadcrumbs_are_never_recorded_with_strict_privacy_on():
    config = Configuration()
    config.strict_privacy = True
    buffer = BreadcrumbBuffer(config)

    buffer.add(category="custom", message="viewed Jane's chart")

    assert buffer.all() == []


class _FakeResponse:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _delivered_trace(monkeypatch, **overrides):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse()

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        trace_capture_threshold_ms=0,
        **overrides,
    )
    now = datetime.datetime.now(datetime.timezone.utc)

    token = forge_ops_tracker._start_span_trace()
    forge_ops_tracker._record_leaf_span("GET https://api.example.com/patients/42?x=1", kind="http", started_at=now, duration_ms=3.0, data={"status": 200})
    forge_ops_tracker._record_database_query("SELECT patients", now, 2.0, "SELECT * FROM patients WHERE id = 8821", db_system="postgresql")
    forge_ops_tracker._finish_span_trace("GET /patients/8821", now, 10.0)
    forge_ops_tracker._end_span_trace(token)

    while True:
        request = delivered.get(timeout=2)
        if request.full_url.endswith("/api/v1/spans"):
            return json.loads(request.data)["spans"]


def test_spans_with_strict_privacy_on_name_calls_by_host_and_keep_only_statement_data(monkeypatch):
    spans = _delivered_trace(monkeypatch, strict_privacy=True)

    by_kind = {span["kind"]: span for span in spans}
    assert by_kind["http"]["name"] == "GET api.example.com"
    assert "data" not in by_kind["http"]
    assert by_kind["database"]["data"] == {"db.statement": "SELECT * FROM patients WHERE id = ?", "db.system": "postgresql"}
    assert by_kind["controller"]["name"] == "GET /patients/:path"
    assert "data" not in by_kind["controller"]


def test_spans_with_strict_privacy_off_are_exactly_as_before(monkeypatch):
    spans = _delivered_trace(monkeypatch)

    by_kind = {span["kind"]: span for span in spans}
    assert by_kind["http"]["name"] == "GET https://api.example.com/patients/42?x=1"
    assert by_kind["http"]["data"] == {"status": 200}
    assert by_kind["controller"]["name"] == "GET /patients/8821"
    assert by_kind["controller"]["data"] == {}
