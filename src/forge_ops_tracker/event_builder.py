"""Turns a raised exception into the payload shape the ingestion API
expects. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/event_builder.rb:
backtrace frames come from traceback.extract_tb rather than regex-parsing
MRI backtrace lines, but the resulting shape (file/line/method/in_app)
is the same.
"""

import sysconfig
import traceback
from datetime import datetime, timezone

from . import pii_scrubber, sql_statement, strict_privacy

MAX_FRAMES = 500

# How many lines of source to grab on either side of the culprit line (see
# EventBuilder._attach_source_context), and the longest a single captured line
# is allowed to be before getting truncated: guards against one pathological
# minified/generated line ballooning the payload. ForgeOps itself re-truncates
# on arrival too, the same "don't just trust the SDK" posture MAX_FRAMES
# already gets on the server side.
CONTEXT_LINES = 5
MAX_CONTEXT_LINE_LENGTH = 500

# Identifies this client to the server's auto language-detection on the project the event lands
# in (see Project#note_sdk_platform server-side); matches this repo's own sdks/python directory
# name, the same convention every other language's client follows.
SDK_NAME = "python"

_STDLIB_PATH = sysconfig.get_path("stdlib")


class EventBuilder:
    def __init__(self, configuration):
        self._configuration = configuration

    # transaction_name/endpoint/trace_id describe the request the error happened in (see
    # request_state.py): transaction_name is the same "GET /orders/<int:id>" name the request's root
    # span and performance samples use, endpoint the human-readable route ("GET /orders/<int:id>"),
    # trace_id the request's 32-character lowercase hex W3C trace id, which is also what links this
    # error to errors other services reported for the same trace. All three are None outside a
    # request (a Celery task, a script), and a None one is left out of the payload entirely.
    #
    # With configuration.strict_privacy on, the finished payload goes through strict_privacy.event:
    # the message is replaced by its message_fingerprint (computed from the message as it would
    # otherwise have been sent, so after scrub_pii, which is also what the server fingerprints from a
    # client without strict privacy, and keeps a message holding an email grouping together), and breadcrumbs, endpoint, the SQL statement, source context, the user's
    # email/username and every context/tag key not in privacy_allowed_fields are left out.
    def build(self, exc, context=None, user=None, breadcrumbs=None, transaction_name=None, endpoint=None, trace_id=None):
        payload = {
            "exception_class": _exception_class_name(exc),
            "message": str(exc),
            "backtrace": self._backtrace(exc),
            "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "environment": self._configuration.environment,
            "release": self._configuration.release,
            "server_name": self._configuration.server_name,
            "context": dict(context) if context else {},
            "tags": {},
            "sdk_name": SDK_NAME,
        }
        if user:
            payload["user"] = dict(user)
        if breadcrumbs:
            payload["breadcrumbs"] = list(breadcrumbs)
        if transaction_name:
            payload["transaction_name"] = transaction_name
        if endpoint:
            payload["endpoint"] = endpoint
        if trace_id:
            payload["trace_id"] = trace_id
        self._attach_sql(payload, exc)

        if self._configuration.scrub_pii:
            payload = self._scrub(payload)

        if self._configuration.strict_privacy:
            payload = strict_privacy.event(payload, self._configuration.privacy_allowed_fields)

        return payload

    # exception_class/occurred_at/environment/release/server_name/sdk_name/user/transaction_name/
    # endpoint/trace_id are left alone: structured fields this client or the host app sets
    # deliberately, not free text an exception or its context could
    # accidentally spill sensitive data into. endpoint in particular is the framework's declared
    # route pattern ("GET /orders/<int:id>"), never the literal path, so there's no id or token in
    # it to scrub. user specifically is a deliberate exemption, not an
    # oversight: the scrubber's own email pattern would otherwise redact the exact thing this
    # field exists to carry. breadcrumbs is *not* exempt, unlike user: console-style/query/request
    # trail entries are exactly the kind of free text (a query's bind params showing up in a
    # message, a URL with a token in it) the scrubber exists to catch, matching
    # gems/forge_ops_tracker's own EventBuilder#scrub treatment of the same field.
    def _scrub(self, payload):
        payload = dict(payload)
        payload["message"] = pii_scrubber.scrub_string(payload["message"])
        payload["backtrace"] = [self._scrub_frame(frame) for frame in payload["backtrace"]]
        payload["context"] = pii_scrubber.scrub(payload["context"])
        payload["tags"] = pii_scrubber.scrub(payload["tags"])
        if "breadcrumbs" in payload:
            payload["breadcrumbs"] = pii_scrubber.scrub(payload["breadcrumbs"])
        if "sql_statement" in payload:
            payload["sql_statement"] = pii_scrubber.scrub_string(payload["sql_statement"])
        return payload

    # See sql_statement.py for what's read off the error and how it's masked. The statement
    # itself only goes out when capture_sql_statement is on; the extracted names go out on their
    # own (capture_sql_objects) so an issue can still name the procedure or view involved.
    def _attach_sql(self, payload, exc):
        config = self._configuration
        if not (config.capture_sql_objects or config.capture_sql_statement):
            return

        masked = sql_statement.mask(sql_statement.find_in(exc))
        if masked is None:
            return

        found = sql_statement.objects(masked)
        if found and config.capture_sql_objects:
            payload["sql_objects"] = found
        if config.capture_sql_statement and not config.strict_privacy:
            payload["sql_statement"] = masked

    def _scrub_frame(self, frame):
        scrubbed = {
            **frame,
            "file": pii_scrubber.scrub_string(frame["file"]) if frame["file"] else frame["file"],
            "method": pii_scrubber.scrub_string(frame["method"]) if frame["method"] else frame["method"],
        }
        if "context_line" in scrubbed:
            scrubbed["context_line"] = pii_scrubber.scrub_string(scrubbed["context_line"])
            scrubbed["pre_context"] = [pii_scrubber.scrub_string(line) for line in scrubbed["pre_context"]]
            scrubbed["post_context"] = [pii_scrubber.scrub_string(line) for line in scrubbed["post_context"]]
        return scrubbed

    def _backtrace(self, exc):
        frames = traceback.extract_tb(exc.__traceback__)[:MAX_FRAMES]
        return [
            self._attach_source_context(
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "method": frame.name,
                    "in_app": self._is_in_app(frame.filename),
                }
            )
            for frame in frames
        ]

    def _is_in_app(self, filename):
        root = self._configuration.app_root
        if not root or not filename:
            return False
        if not filename.startswith(root):
            return False
        if _STDLIB_PATH and filename.startswith(_STDLIB_PATH):
            return False
        return "site-packages" not in filename and "dist-packages" not in filename

    # Reads a few lines of source straight off disk around the culprit line,
    # at raise-time, in the same running process the exception came from.
    # Gated on two things: the frame has to be in-app (never a vendored
    # dependency: there'd be nothing meaningful to show, and it's not the
    # host app's own code to begin with), and
    # configuration.capture_source_context has to be True (see Configuration
    # for why it defaults to True and why ForgeOps' own per-project setting,
    # not this flag, is the durable, protected way to turn it off). Uses a
    # plain open()/readlines() rather than the stdlib's linecache module:
    # linecache caches file contents for the lifetime of the process, which
    # is wrong here since a long-running server could keep reading a stale
    # copy of a file that's since changed on disk. Best-effort: any file that
    # can't be read (deleted, permission denied, a path that only ever
    # existed inside a build step and isn't present in this deployment) just
    # means this one frame gets no source context, never a raised error of
    # its own. Never read at all under strict privacy, which never sends it.
    def _attach_source_context(self, frame):
        if not self._configuration.capture_source_context or not frame["in_app"] or self._configuration.strict_privacy:
            return frame

        try:
            with open(frame["file"]) as source_file:
                lines = source_file.readlines()
        except (OSError, UnicodeDecodeError):
            return frame

        index = frame["line"] - 1
        if not (0 <= index < len(lines)):
            return frame

        from_index = max(index - CONTEXT_LINES, 0)
        to_index = min(index + CONTEXT_LINES, len(lines) - 1)

        return {
            **frame,
            "context_line": _truncate_line(lines[index]),
            "pre_context": [_truncate_line(line) for line in lines[from_index:index]],
            "post_context": [_truncate_line(line) for line in lines[index + 1 : to_index + 1]],
        }


def _exception_class_name(exc):
    cls = type(exc)
    if cls.__module__ in ("builtins", "__main__"):
        return cls.__name__
    return f"{cls.__module__}.{cls.__name__}"


def _truncate_line(line):
    line = line.rstrip("\n")
    if len(line) <= MAX_CONTEXT_LINE_LENGTH:
        return line
    return f"{line[:MAX_CONTEXT_LINE_LENGTH]}..."
