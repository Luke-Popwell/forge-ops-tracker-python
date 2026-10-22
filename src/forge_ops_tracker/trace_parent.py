"""Reads and writes the W3C Trace Context `traceparent` header
(https://www.w3.org/TR/trace-context/), the vendor-neutral format for carrying one trace across
service boundaries: `00-<32 hex trace-id>-<16 hex parent-id>-<2 hex flags>`. Used in both
directions: the Django/Flask integrations parse an incoming one so a request continues the caller's
trace instead of starting its own, and integrations/requests.py builds an outgoing one so the next
service along continues this request's. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/trace_parent.rb.

Deliberately strict on the way in, the same posture the spec itself asks receivers to take: a
malformed value, uppercase hex, the reserved version "ff", or an all-zero trace/parent id are all
treated as "no usable header at all" (parse returns None and the request starts a fresh trace),
never half-trusted. A version this SDK doesn't know yet is still accepted as long as its first four
fields have version 00's shape, which is exactly what the spec says a version-00 parser should do
with a future version; version 00 itself must have exactly four fields.
"""

import re
import secrets

HEADER = "traceparent"

_PATTERN = re.compile(r"\A(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})(?P<rest>-.*)?\Z", re.DOTALL)
_INVALID_TRACE_ID = "0" * 32
_INVALID_PARENT_ID = "0" * 16

# Always "01" (sampled) on the way out: this SDK decides whether to actually send a trace only once
# the request is over (see forge_ops_tracker._finish_span_trace), long after this header has already
# gone out on an outbound call, so there's no honest earlier answer to give than "this may be
# recorded." A downstream service is free to make its own decision either way.
_SAMPLED_FLAGS = "01"


def parse(value):
    """Returns (trace_id, parent_span_id) for a usable header, None for anything else (absent,
    blank, malformed, or one of the explicitly invalid values above)."""
    if not isinstance(value, str):
        return None

    match = _PATTERN.match(value.strip())
    if match is None:
        return None
    if match.group("version") == "ff":
        return None
    if match.group("version") == "00" and match.group("rest") is not None:
        return None
    if match.group("trace_id") == _INVALID_TRACE_ID or match.group("parent_id") == _INVALID_PARENT_ID:
        return None

    return match.group("trace_id"), match.group("parent_id")


def build(trace_id, span_id):
    return f"00-{trace_id}-{span_id}-{_SAMPLED_FLAGS}"


def generate_trace_id():
    """32 lowercase hex characters, the W3C trace-id format, never all zeros (the spec reserves
    that as invalid, and a receiver drops the whole header over it): vanishingly unlikely from 16
    random bytes, but free to rule out."""
    return _random_id(16)


def generate_span_id():
    """16 lowercase hex characters, the W3C parent-id (span id) format, never all zeros."""
    return _random_id(8)


def _random_id(nbytes):
    # secrets.token_hex already produces lowercase, the only case the spec allows.
    value = secrets.token_hex(nbytes)
    while value.strip("0") == "":
        value = secrets.token_hex(nbytes)
    return value
