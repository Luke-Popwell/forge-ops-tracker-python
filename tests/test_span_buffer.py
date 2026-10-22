import contextvars
import re
from datetime import datetime, timezone

from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.span_buffer import (
    SpanBuffer,
    generate_span_id,
    generate_trace_id,
)

ISO_UTC_MS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _configuration():
    config = Configuration()
    config.environment = "production"
    config.release = "v1.2.3"
    return config


def test_generates_a_fresh_trace_id_and_root_span_id_on_construction():
    buffer = SpanBuffer(_configuration())

    assert buffer.trace_id
    assert buffer.root_span_id
    assert buffer.trace_id != buffer.root_span_id


def test_generate_trace_id_and_generate_span_id_return_distinct_values_each_call():
    assert generate_trace_id() != generate_trace_id()
    assert generate_span_id() != generate_span_id()


def test_a_leaf_span_recorded_before_the_root_nests_under_the_root_automatically():
    # The root span_id is pushed onto the stack at construction, specifically so a query/HTTP
    # span recorded before the request itself finishes (which is every one of them) nests under
    # it without the root having to exist as a real recorded span yet.
    buffer = SpanBuffer(_configuration())

    buffer.record(span_id="q1", name="SELECT users", kind="database", started_at=datetime.now(timezone.utc), duration_ms=5.0)

    assert buffer.spans[0]["parent_span_id"] == buffer.root_span_id


def test_pushing_a_span_id_nests_subsequent_leaf_spans_under_it():
    buffer = SpanBuffer(_configuration())

    token = buffer.push("service-1")
    buffer.record(span_id="q1", name="SELECT users", kind="database", started_at=datetime.now(timezone.utc), duration_ms=5.0)
    buffer.pop(token)
    buffer.record(span_id="q2", name="SELECT posts", kind="database", started_at=datetime.now(timezone.utc), duration_ms=5.0)

    assert buffer.spans[0]["parent_span_id"] == "service-1"
    assert buffer.spans[1]["parent_span_id"] == buffer.root_span_id


def test_root_true_forces_parent_span_id_to_none_rather_than_reading_the_stack():
    buffer = SpanBuffer(_configuration())

    buffer.record(
        span_id=buffer.root_span_id, name="GET /checkout", kind="controller",
        started_at=datetime.now(timezone.utc), duration_ms=1800.0, root=True,
    )

    assert buffer.spans[0]["parent_span_id"] is None


def test_record_formats_started_at_as_iso8601_with_millisecond_precision_utc():
    buffer = SpanBuffer(_configuration())

    buffer.record(span_id="s1", name="x", kind="other", started_at=datetime(2026, 1, 1, 10, 0, 0, 123000, tzinfo=timezone.utc), duration_ms=1.0)

    assert buffer.spans[0]["started_at"] == "2026-01-01T10:00:00.123Z"
    assert ISO_UTC_MS_RE.match(buffer.spans[0]["started_at"])


def test_record_carries_the_configured_environment_and_release_on_every_span():
    buffer = SpanBuffer(_configuration())

    buffer.record(span_id="s1", name="x", kind="other", started_at=datetime.now(timezone.utc), duration_ms=1.0)

    assert buffer.spans[0]["environment"] == "production"
    assert buffer.spans[0]["release"] == "v1.2.3"


def test_data_defaults_to_an_empty_dict_when_not_given():
    buffer = SpanBuffer(_configuration())

    buffer.record(span_id="s1", name="x", kind="other", started_at=datetime.now(timezone.utc), duration_ms=1.0)

    assert buffer.spans[0]["data"] == {}


def test_is_slow_is_false_until_the_root_span_has_been_recorded():
    buffer = SpanBuffer(_configuration())

    assert buffer.is_slow(1000) is False


def test_is_slow_compares_only_the_roots_own_duration_against_the_threshold():
    buffer = SpanBuffer(_configuration())
    buffer.record(span_id=buffer.root_span_id, name="GET /checkout", kind="controller", started_at=datetime.now(timezone.utc), duration_ms=1800.0, root=True)

    assert buffer.is_slow(1000) is True
    assert buffer.is_slow(2000) is False


def test_the_root_is_the_parent_whenever_nothing_else_is_open():
    buffer = SpanBuffer(_configuration())
    assert buffer.current_parent_id == buffer.root_span_id

    outer = buffer.push("service-1")
    inner = buffer.push("service-2")
    assert buffer.current_parent_id == "service-2"
    buffer.pop(inner)
    assert buffer.current_parent_id == "service-1"
    buffer.pop(outer)

    assert buffer.current_parent_id == buffer.root_span_id


def test_a_span_opened_in_a_copied_context_stays_in_that_context():
    buffer = SpanBuffer(_configuration())
    token = buffer.push("service-1")

    def child():
        assert buffer.current_parent_id == "service-1"  # inherits what was open when it was copied
        buffer.push("child-1")
        return buffer.current_parent_id

    assert contextvars.copy_context().run(child) == "child-1"
    assert buffer.current_parent_id == "service-1"  # the child's push never leaks back
    buffer.pop(token)


def test_another_buffers_open_spans_are_never_read_as_this_ones():
    first = SpanBuffer(_configuration())
    token = first.push("service-1")

    second = SpanBuffer(_configuration())

    assert second.current_parent_id == second.root_span_id
    first.pop(token)


def test_pop_restores_the_previous_value_even_from_a_context_other_than_the_one_that_pushed():
    buffer = SpanBuffer(_configuration())
    token = contextvars.copy_context().run(buffer.push, "service-1")

    buffer.pop(token)  # a token from another context can't be reset here, so its old value is set instead

    assert buffer.current_parent_id == buffer.root_span_id


def test_uses_a_given_trace_id_and_records_the_root_under_a_remote_parent():
    buffer = SpanBuffer(_configuration(), trace_id="4bf92f3577b34da6a3ce929d0e0e4736", parent_span_id="00f067aa0ba902b7")
    buffer.record(span_id=buffer.root_span_id, name="GET /checkout", kind="controller", started_at=datetime.now(timezone.utc), duration_ms=5.0, root=True)

    assert buffer.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert buffer.spans[0]["parent_span_id"] == "00f067aa0ba902b7"


def test_generated_ids_are_w3c_shaped():
    buffer = SpanBuffer(_configuration())

    assert re.fullmatch(r"[0-9a-f]{32}", buffer.trace_id)
    assert re.fullmatch(r"[0-9a-f]{16}", buffer.root_span_id)


def test_root_recorded_is_false_until_the_root_span_is_recorded():
    buffer = SpanBuffer(_configuration())
    assert buffer.root_recorded is False

    buffer.record(span_id=buffer.root_span_id, name="GET /checkout", kind="controller", started_at=datetime.now(timezone.utc), duration_ms=5.0, root=True)
    assert buffer.root_recorded is True
