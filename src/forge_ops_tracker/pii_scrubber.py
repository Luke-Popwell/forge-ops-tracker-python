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
    "iban", "accountnumber", "routingnumber", "bankaccount",
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

# Card numbers in any other shape, and IBANs: see sdks/PII_SCRUBBING.md, which every SDK follows,
# and sdks/pii_scrubbing_vectors.json (a copy is in tests/fixtures), which pins the results.
# re.ASCII keeps \b and \d to ASCII word characters and digits, the same in every SDK.
_DIGIT_RUN = re.compile(r"(?<![A-Za-z0-9_])[0-9](?:[ -]?[0-9])*(?![A-Za-z0-9_])")
_CARD_PREFIXES = re.compile(
    r"(?:4|5[1-5]|2(?:22[1-9]|2[3-9][0-9]|[3-6][0-9][0-9]|7[01][0-9]|720)|3[47]|6011|64[4-9]|65"
    r"|35(?:2[89]|[3-8][0-9])|36|30[0-5]|62)"
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,4})?)\b", re.ASCII)


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
    # IBANs first: a spaced IBAN's middle groups look like a formatted card number.
    text = _IBAN.sub(lambda match: "[IBAN FILTERED]" if _is_iban(match.group(0)) else match.group(0), text)
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[{label} FILTERED]", text)
    return _DIGIT_RUN.sub(lambda match: _scrub_card_numbers_in(match.group(0)), text)


# From each group of the run on, the longest stretch of whole groups that is a card number is
# replaced, so a card followed by its CVV, two cards in a row, and Amex's 4-6-5 are all found.
def _scrub_card_numbers_in(run):
    parts = re.split(r"([ -])", run)
    digits = parts[0::2]
    separators = parts[1::2] + [""]
    out = []
    i = 0
    while i < len(digits):
        j = _last_card_group(digits, i)
        if j is not None:
            out.append("[CREDIT CARD FILTERED]" + separators[j])
            i = j + 1
        else:
            out.append(digits[i] + separators[i])
            i += 1
    return "".join(out)


def _last_card_group(digits, first):
    for last in range(len(digits) - 1, first - 1, -1):
        if _is_card_number("".join(digits[first:last + 1])):
            return last
    return None


def _is_card_number(digits):
    return 13 <= len(digits) <= 19 and _CARD_PREFIXES.match(digits) is not None and _luhn(digits)


def _luhn(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# The IBAN check, a digit at a time: the first four characters moved to the end, letters as two
# digits (A is 10), and the remainder by 97 is 1.
def _is_iban(candidate):
    compact = candidate.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    remainder = 0
    for char in compact[4:] + compact[:4]:
        for digit in str(ord(char) - 55) if "A" <= char <= "Z" else char:
            remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


def _is_sensitive_key(key):
    if not key:
        return False
    normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
    return any(sensitive in normalized for sensitive in SENSITIVE_KEYS)
