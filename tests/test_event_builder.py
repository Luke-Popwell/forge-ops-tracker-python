import os
import re

from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.event_builder import EventBuilder

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _configuration():
    config = Configuration()
    config.environment = "production"
    config.release = "abc123"
    config.server_name = "web-1"
    return config


def _raise_and_capture():
    try:
        raise ValueError("boom")
    except ValueError as e:
        return e


def test_builds_a_payload_with_the_exception_class_message_and_configured_metadata():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()

    payload = builder.build(error, context={"url": "https://example.com"})

    assert payload["exception_class"] == "ValueError"
    assert payload["message"] == "boom"
    assert payload["environment"] == "production"
    assert payload["release"] == "abc123"
    assert payload["server_name"] == "web-1"
    assert payload["context"] == {"url": "https://example.com"}
    assert payload["sdk_name"] == "python"
    assert ISO_UTC_RE.match(payload["occurred_at"])


def test_uses_the_full_dotted_path_for_a_non_builtin_exception_class():
    class PaymentError(Exception):
        pass

    try:
        raise PaymentError("declined")
    except PaymentError as error:
        payload = EventBuilder(_configuration()).build(error)

    assert payload["exception_class"] == f"{__name__}.PaymentError"


def test_parses_real_stack_frames_with_file_line_and_method():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()

    frames = builder.build(error)["backtrace"]

    assert len(frames) >= 1
    frame = frames[-1]  # innermost frame: where the raise actually happened
    assert frame["file"] == os.path.abspath(__file__)
    assert frame["method"] == "_raise_and_capture"
    assert isinstance(frame["line"], int)


def test_marks_a_frame_under_app_root_as_in_app():
    config = _configuration()
    config.app_root = THIS_DIR
    error = _raise_and_capture()

    frames = EventBuilder(config).build(error)["backtrace"]

    assert any(frame["in_app"] for frame in frames)


def test_marks_every_frame_as_not_in_app_when_app_root_does_not_match():
    config = _configuration()
    config.app_root = "/definitely/not/here"
    error = _raise_and_capture()

    frames = EventBuilder(config).build(error)["backtrace"]

    assert all(not frame["in_app"] for frame in frames)


def test_returns_an_empty_backtrace_when_the_exception_was_never_raised():
    builder = EventBuilder(_configuration())
    error = ValueError("boom")  # constructed, never raised: no traceback to capture

    assert builder.build(error)["backtrace"] == []


def test_scrubs_likely_pii_out_of_the_message_and_context_by_default():
    builder = EventBuilder(_configuration())
    try:
        raise ValueError("failed for user@example.com")
    except ValueError as error:
        payload = builder.build(
            error,
            context={"user": {"email": "ada@example.com", "password": "hunter2"}},
        )

    assert payload["message"] == "failed for [EMAIL FILTERED]"
    assert payload["context"] == {"user": {"email": "[EMAIL FILTERED]", "password": "[FILTERED]"}}


def test_includes_the_user_when_given_one_never_scrubbed_even_though_its_an_email():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()

    payload = builder.build(error, user={"id": 42, "email": "ada@example.com"})

    assert payload["user"] == {"id": 42, "email": "ada@example.com"}


def test_omits_the_user_key_entirely_when_none_was_given():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()

    payload = builder.build(error)

    assert "user" not in payload


def test_includes_breadcrumbs_when_given_some():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()
    breadcrumbs = [{"category": "query", "message": "User Load", "level": "info", "timestamp": "2026-01-01T00:00:00Z", "data": {}}]

    payload = builder.build(error, breadcrumbs=breadcrumbs)

    assert payload["breadcrumbs"] == breadcrumbs


def test_omits_the_breadcrumbs_key_entirely_when_none_was_given():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()

    payload = builder.build(error)

    assert "breadcrumbs" not in payload


def test_omits_the_breadcrumbs_key_entirely_when_given_an_empty_list():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()

    payload = builder.build(error, breadcrumbs=[])

    assert "breadcrumbs" not in payload


def test_scrubs_breadcrumb_messages_and_data_unlike_user_since_a_breadcrumb_is_not_a_deliberately_structured_field():
    builder = EventBuilder(_configuration())
    error = _raise_and_capture()
    breadcrumbs = [
        {
            "category": "query",
            "message": "failed for user@example.com",
            "level": "info",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"password": "hunter2"},
        }
    ]

    payload = builder.build(error, breadcrumbs=breadcrumbs)

    assert payload["breadcrumbs"][0]["message"] == "failed for [EMAIL FILTERED]"
    assert payload["breadcrumbs"][0]["data"] == {"password": "[FILTERED]"}


def test_leaves_the_payload_untouched_when_scrub_pii_is_disabled():
    config = _configuration()
    config.scrub_pii = False
    builder = EventBuilder(config)
    try:
        raise ValueError("failed for user@example.com")
    except ValueError as error:
        payload = builder.build(error, context={"email": "ada@example.com"})

    assert payload["message"] == "failed for user@example.com"
    assert payload["context"] == {"email": "ada@example.com"}


# Source context capture: _attach_source_context is exercised directly with a
# hand-built frame dict, rather than through a real raised exception, since
# what's under test is purely "given a file/line, what does it read off
# disk": the same thing gems/forge_ops_tracker/spec/.../event_builder_spec.rb
# tests by stubbing `error.backtrace` to an arbitrary "file:line:in 'call'"
# string rather than actually raising from that file.
def _write_source_file(tmp_path, line_count, name="app_file.py"):
    path = tmp_path / name
    path.write_text("\n".join(f"line {n}" for n in range(1, line_count + 1)))
    return str(path)


def _frame_for(builder, file_path, line):
    frame = {"file": file_path, "line": line, "method": "call", "in_app": True}
    return builder._attach_source_context(frame)


def test_attaches_context_line_pre_context_and_post_context_around_an_in_app_frames_culprit_line_by_default(tmp_path):
    builder = EventBuilder(_configuration())
    file_path = _write_source_file(tmp_path, 20)

    frame = _frame_for(builder, file_path, 10)

    assert frame["context_line"] == "line 10"
    assert frame["pre_context"] == [f"line {n}" for n in range(5, 10)]
    assert frame["post_context"] == [f"line {n}" for n in range(11, 16)]


def test_clamps_pre_context_and_post_context_at_the_start_and_end_of_the_file_rather_than_raising(tmp_path):
    builder = EventBuilder(_configuration())
    file_path = _write_source_file(tmp_path, 3)

    first_frame = _frame_for(builder, file_path, 1)
    last_frame = _frame_for(builder, file_path, 3)

    assert first_frame["pre_context"] == []
    assert first_frame["post_context"] == ["line 2", "line 3"]
    assert last_frame["pre_context"] == ["line 1", "line 2"]
    assert last_frame["post_context"] == []


def test_truncates_an_individual_line_longer_than_max_context_line_length(tmp_path):
    builder = EventBuilder(_configuration())
    overlong = "x" * 600
    path = tmp_path / "app_file.py"
    path.write_text(overlong)

    frame = _frame_for(builder, str(path), 1)

    assert frame["context_line"] == f"{'x' * 500}..."


def test_never_attaches_context_to_a_frame_that_is_not_in_app(tmp_path):
    builder = EventBuilder(_configuration())
    file_path = _write_source_file(tmp_path, 20)

    frame = builder._attach_source_context({"file": file_path, "line": 10, "method": "call", "in_app": False})

    assert "context_line" not in frame
    assert "pre_context" not in frame
    assert "post_context" not in frame


def test_leaves_a_frame_untouched_with_no_file_read_attempted_when_capture_source_context_is_disabled(
    tmp_path, monkeypatch
):
    config = _configuration()
    config.capture_source_context = False
    builder = EventBuilder(config)
    file_path = _write_source_file(tmp_path, 20)

    def _unexpected_open(*args, **kwargs):
        raise AssertionError("open() should not be called when capture_source_context is disabled")

    monkeypatch.setattr("builtins.open", _unexpected_open)

    frame = _frame_for(builder, file_path, 10)

    assert "context_line" not in frame


def test_leaves_a_frame_untouched_when_the_recorded_file_cannot_be_read(tmp_path):
    builder = EventBuilder(_configuration())
    missing_path = str(tmp_path / "this-file-does-not-exist.py")

    frame = _frame_for(builder, missing_path, 1)

    assert "context_line" not in frame


def test_includes_the_request_trace_fields_when_given():
    builder = EventBuilder(_configuration())

    payload = builder.build(
        _raise_and_capture(),
        transaction_name="GET /orders/<int:order_id>",
        endpoint="GET /orders/<int:order_id>",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
    )

    assert payload["transaction_name"] == "GET /orders/<int:order_id>"
    assert payload["endpoint"] == "GET /orders/<int:order_id>"
    assert payload["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_omits_the_request_trace_fields_entirely_when_not_given():
    payload = EventBuilder(_configuration()).build(_raise_and_capture())

    assert "transaction_name" not in payload
    assert "endpoint" not in payload
    assert "trace_id" not in payload


def test_never_scrubs_the_request_trace_fields():
    config = _configuration()
    config.scrub_pii = True

    payload = EventBuilder(config).build(
        _raise_and_capture(),
        transaction_name="POST /users/<email>/confirm",
        endpoint="POST /users/<email>/confirm",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
    )

    assert payload["endpoint"] == "POST /users/<email>/confirm"
    assert payload["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
