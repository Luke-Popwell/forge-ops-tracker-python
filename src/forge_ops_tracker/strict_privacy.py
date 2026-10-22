"""What Configuration.strict_privacy changes in everything this client sends, for apps that can't
let personal or health data leave their own servers. With it on, only structured facts go out (the
error's class, its stack frames, release, environment, server, which request it happened in,
timings and counts); free text, which is where a name, an email or a diagnosis hides, never leaves
this process: no error message, no breadcrumbs, no endpoint, no SQL statement, no source context,
no user email or username, and only the context/tag keys listed in
Configuration.privacy_allowed_fields.

The same rules as the server's own StrictPrivacy (app/services/strict_privacy.rb), which applies
them again on arrival when the project's Strict privacy setting is on; sdks/strict_privacy_vectors.json
pins the results both have to agree on, byte for byte.
"""

import hashlib
import re

FINGERPRINT_LENGTH = 32

# ASCII "word" characters; boundaries are defined against these, the same in every SDK, so Python's
# own idea of a word (Unicode letters, digits in other scripts) never changes the result. re.ASCII
# too, though every class here is spelled out explicitly anyway.
_WORD = "A-Za-z0-9_"
_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.ASCII)
# A quoted value (KeyError: 'Jane Doe') is data, not the shape of the error. Single quotes only open
# and close at a word boundary, so a contraction (Couldn't) or the `email' style never starts one.
_DOUBLE_QUOTED = re.compile(r'"[^"\r\n]*"', re.ASCII)
_SINGLE_QUOTED = re.compile(rf"(?<![{_WORD}])'[^'\r\n]*'(?![{_WORD}])", re.ASCII)
_HEX_OBJECT_ID = re.compile(r"0x[0-9a-fA-F]{4,}", re.ASCII)
_UUID = re.compile(
    rf"(?<![{_WORD}])[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}(?![{_WORD}])",
    re.ASCII,
)
_NUMBER = re.compile(rf"(?<![{_WORD}])[0-9]+(?![{_WORD}])", re.ASCII)

# Span data that's structure, not content: the masked statement and which database it ran on.
SPAN_DATA_KEYS = ("db.statement", "db.system")
_HTTP_METHOD = re.compile(r"\A(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)", re.IGNORECASE | re.ASCII)
_SCHEME = re.compile(r"\A[a-z][a-z0-9+.-]*://", re.IGNORECASE | re.ASCII)
_DIGIT = re.compile(r"[0-9]")
_ROUTE_PARAMETER = re.compile(r"(?::.+|\{.+\}|<.+>|\[.+\]|\*.*)")

# Error event fields that are never sent, and the per-frame source context.
_EVENT_FIELDS_DROPPED = ("message", "breadcrumbs", "endpoint", "sql_statement")
_SOURCE_CONTEXT_FIELDS = ("context_line", "pre_context", "post_context")


def normalize_message(message):
    """The message with emails, quoted values, hex object ids, UUIDs and standalone numbers replaced,
    in that order."""
    text = "" if message is None else str(message)
    text = _EMAIL.sub("<email>", text)
    text = _DOUBLE_QUOTED.sub("<str>", text)
    text = _SINGLE_QUOTED.sub("<str>", text)
    text = _HEX_OBJECT_ID.sub("0x", text)
    text = _UUID.sub("<uuid>", text)
    return _NUMBER.sub("<n>", text)


def message_fingerprint(message):
    """Lets ForgeOps group errors without ever seeing their message: the first 32 hex characters of
    the SHA-256 of the normalized message's UTF-8 bytes, so two messages that differ only in ids
    hash the same, and nothing about the text can be read back from it. A lone surrogate (which
    UTF-8 can't encode) is hashed as U+FFFD."""
    normalized = normalize_message(message).encode("utf-8", "replace")
    return hashlib.sha256(normalized).hexdigest()[:FINGERPRINT_LENGTH]


def event(payload, allowed_fields=()):
    """An error event payload (EventBuilder.build's shape) reduced to what strict privacy sends.
    message is sent as "" alongside message_fingerprint, exactly what the server stores for a
    strict project."""
    reduced = {key: value for key, value in payload.items() if key not in _EVENT_FIELDS_DROPPED}
    reduced["message"] = ""
    reduced["message_fingerprint"] = message_fingerprint(payload.get("message"))
    reduced["transaction_name"] = transaction_name(payload.get("transaction_name"))
    reduced["context"] = _allowed(payload.get("context"), allowed_fields)
    reduced["tags"] = _allowed(payload.get("tags"), allowed_fields)
    reduced["user"] = _user(payload.get("user"))
    reduced["backtrace"] = [
        {key: value for key, value in frame.items() if key not in _SOURCE_CONTEXT_FIELDS} for frame in payload.get("backtrace") or []
    ]
    return {key: value for key, value in reduced.items() if value is not None}


def span(payload):
    """A trace's span: an outbound call's name keeps its method and host, never its path or query,
    and its data keeps only the masked statement and database system. data is left out entirely
    when nothing is left of it, the same as the server does."""
    reduced = dict(payload)
    reduced["name"] = span_name(payload.get("name"))
    data = {key: value for key, value in (payload.get("data") or {}).items() if key in SPAN_DATA_KEYS}
    if data:
        reduced["data"] = data
    else:
        reduced.pop("data", None)
    return {key: value for key, value in reduced.items() if value is not None}


def span_name(name):
    """"GET https://api.example.com/patients/42?x=1" or "GET api.example.com/patients/42" becomes
    "GET api.example.com". A bare path ("/patients/42"), or a method and a path
    ("PUT /orders/17/items"), goes through the path rule, like a page path. A name that's neither
    ("OrdersController#create", "patients#show") is kept."""
    if name is not None and str(name).startswith("/"):
        return strict_path(name)

    match = _HTTP_METHOD.match("" if name is None else str(name))
    if match is None:
        return name

    method = match.group(1).upper()
    target = match.group(2)
    if target.startswith("/"):
        return f"{method} {strict_path(target)}"
    return f"{method} {_host(target) or strict_path(target)}"


def transaction_name(name):
    """Which request something happened in: a route or a view is kept, but a raw path (what a
    request no route matched falls back to) keeps only its first segment, by the span name rule."""
    return span_name(name) if name else name


def strict_path(path):
    """A request or page path under strict privacy, with no query or fragment. A route template
    ("/patients/:id/notes", "/patients/{id}", "users/<int:id>/", "/files/*path", "/posts/[slug]") is
    kept as it is: its words are a developer's, and its parameters are already placeholders. A raw
    path keeps only its first segment, and the rest becomes ":path": "/patients/jane-doe/12" is
    "/patients/:path". Anything after the first segment of a raw path could name a person, and no
    rule can tell a name from a word ("/patients/janedoe"). A first segment that holds a digit or is
    over 40 characters is ":id" itself."""
    path = re.split(r"[?#]", "" if path is None else str(path), maxsplit=1)[0]
    if _is_route_template(path):
        return path

    leading = "/" if path.startswith("/") else ""
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return "/"

    first = ":id" if _DIGIT.search(segments[0]) or len(segments[0]) > 40 else segments[0]
    return f"{leading}{first}{'/:path' if len(segments) > 1 else ''}"


def _is_route_template(path):
    # Whether any segment is a route parameter: ":id", "{id}", "<int:id>", "[slug]" or "*path".
    return any(_ROUTE_PARAMETER.fullmatch(segment) for segment in path.split("/"))


def _host(target):
    # An outbound call's host, read by hand rather than with a URL parser (an address a parser
    # rejects must never be sent whole): what comes before the first "/", "?" or "#", without a
    # scheme, credentials (up to the last "@", since a password can hold one) or port. Only a host
    # when the address had a scheme or a port, or the name has a dot or is "localhost"; None
    # otherwise (a relative path).
    scheme = _SCHEME.match(target)
    rest = target[scheme.end() :] if scheme else target
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    authority = re.sub(r"\A.*@", "", authority, count=1, flags=re.DOTALL)
    port = re.search(r":[0-9]+\Z", authority)
    host = authority[: port.start()] if port else authority
    if not host:
        return None
    if scheme or port or "." in host or host.lower() == "localhost":
        return host
    return None


def _allowed(fields, allowed_fields):
    allowed_fields = [str(field) for field in allowed_fields or ()]
    if not allowed_fields or not isinstance(fields, dict):
        return None

    kept = {
        str(key): value
        for key, value in fields.items()
        if str(key) in allowed_fields and isinstance(value, (str, int, float, bool))
    }
    return kept or None


def _user(user):
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    if user_id is None or str(user_id).strip() == "":
        return None
    return {"id": str(user_id)}
