"""Redacts likely-sensitive content out of a payload before it ever leaves
this process: the same patterns ForgeOps itself applies again on
arrival (defense in depth: this layer keeps the data off the wire and out
of any request logging in between; the server-side layer is what actually
protects the database, and doesn't depend on every reporting app running
an up-to-date version of this client). Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/pii_scrubber.rb: kept
standalone and dependency-free here for the same reason as the Ruby
original: this has to work in any host app regardless of what's reporting
into it.

Can be turned off via Configuration.scrub_pii = False for a host app that
already scrubs its own data before it ever reaches exception context, or
that has its own reasons to want the raw payload. Off by default is not
an option: the safe default has to be "on."
"""

import re

REDACTED = "[FILTERED]"

SENSITIVE_KEYS = {
    "password", "passwd", "pwd",
    "secret", "apisecret", "clientsecret", "secretkey",
    "token", "accesstoken", "refreshtoken", "apikey", "apitoken", "authorization", "authtoken", "bearer",
    "sessiontoken", "csrftoken",
    "creditcard", "cardnumber", "cardnum", "cvv", "cvv2", "cvc",
    "ssn", "socialsecuritynumber", "socialsecurity",
    "privatekey",
}

_PATTERNS = (
    ("EMAIL", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT CARD", re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,4}\b")),
    ("BEARER TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)),
    ("JWT", re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("AWS KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("STRIPE KEY", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}\b")),
    ("GITHUB TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
)


def scrub(value, key=None):
    if _is_sensitive_key(key) and value is not None:
        return REDACTED

    if isinstance(value, dict):
        return {k: scrub(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, key=key) for v in value]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def scrub_string(text):
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[{label} FILTERED]", text)
    return text


def _is_sensitive_key(key):
    if not key:
        return False
    normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
    return any(sensitive in normalized for sensitive in SENSITIVE_KEYS)
