import re

import pytest

from forge_ops_tracker import trace_parent

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"


def test_parses_a_valid_version_00_header():
    assert trace_parent.parse(f"00-{TRACE_ID}-{PARENT_ID}-01") == (TRACE_ID, PARENT_ID)


def test_accepts_an_unsampled_flag_and_surrounding_whitespace():
    assert trace_parent.parse(f"  00-{TRACE_ID}-{PARENT_ID}-00 ") == (TRACE_ID, PARENT_ID)


def test_accepts_a_future_version_with_extra_fields():
    assert trace_parent.parse(f"cc-{TRACE_ID}-{PARENT_ID}-01-what-the-future-holds") == (TRACE_ID, PARENT_ID)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        12345,
        "garbage",
        f"00-{TRACE_ID}-{PARENT_ID}",  # no flags
        f"00-{TRACE_ID.upper()}-{PARENT_ID}-01",  # uppercase hex
        f"00-{TRACE_ID}-{PARENT_ID.upper()}-01",
        f"ff-{TRACE_ID}-{PARENT_ID}-01",  # reserved version
        f"00-{'0' * 32}-{PARENT_ID}-01",  # all-zero trace id
        f"00-{TRACE_ID}-{'0' * 16}-01",  # all-zero parent id
        f"00-{TRACE_ID}-{PARENT_ID}-01-extra",  # extra field on version 00
        f"00-{TRACE_ID[:-1]}-{PARENT_ID}-01",  # short trace id
        f"00-{TRACE_ID}-{PARENT_ID}0-01",  # long parent id
        f"0-{TRACE_ID}-{PARENT_ID}-01",
        f"00-{TRACE_ID}-{PARENT_ID}-1",
        f"00_{TRACE_ID}_{PARENT_ID}_01",
        f"00-{TRACE_ID}-{PARENT_ID}-0\n1",  # embedded newline
    ],
)
def test_rejects_anything_that_is_not_a_usable_header(value):
    assert trace_parent.parse(value) is None


def test_builds_a_sampled_version_00_header():
    assert trace_parent.build(TRACE_ID, PARENT_ID) == f"00-{TRACE_ID}-{PARENT_ID}-01"


def test_generated_ids_are_w3c_shaped_lowercase_hex():
    for _ in range(50):
        assert re.fullmatch(r"[0-9a-f]{32}", trace_parent.generate_trace_id())
        assert re.fullmatch(r"[0-9a-f]{16}", trace_parent.generate_span_id())


def test_never_generates_an_all_zero_id(monkeypatch):
    values = iter(["0" * 16, "0" * 16, "00000000000000a1"])
    monkeypatch.setattr("forge_ops_tracker.trace_parent.secrets.token_hex", lambda nbytes: next(values))

    assert trace_parent.generate_span_id() == "00000000000000a1"


def test_a_generated_header_parses_back():
    trace_id = trace_parent.generate_trace_id()
    span_id = trace_parent.generate_span_id()

    assert trace_parent.parse(trace_parent.build(trace_id, span_id)) == (trace_id, span_id)
