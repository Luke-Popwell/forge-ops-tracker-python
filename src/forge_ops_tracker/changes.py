"""Payloads for "what changed": one explicit record_change() call (POST /api/v1/changes), and the
startup snapshot (POST /api/v1/change_snapshots) that ForgeOps diffs against the previous boot's to
record what changed between deploys. Mirrors gems/forge_ops_tracker's Change and ChangeSnapshot.

The snapshot leaves out any key it can't determine reliably rather than guessing, since the server
reads a missing key as "unknown", never as "everything was removed". Environment variable names are
only ever names, never values, and only when track_env_var_names is on.
"""

import os
import platform
import re
from datetime import datetime, timezone

# The server rejects any other kind outright (422), so an unknown one is sent as "other" rather than
# dropped: the change still gets recorded, just less specifically categorized.
KINDS = ("feature_flag", "config", "migration", "dependency", "infrastructure", "other")
MAX_TITLE_LENGTH = 200

MAX_DEPENDENCIES = 3000
MAX_NAME_LENGTH = 200
MAX_VERSION_LENGTH = 100
MAX_ENV_VAR_NAMES = 3000

# Host-specific variables that differ from one box to the next (or one boot to the next) without
# anything about the deploy having changed, so a fleet doesn't look like it's changing on every
# restart. The SDK's own FORGE_OPS_* settings are dropped too.
ENV_VAR_DENYLIST = frozenset(
    {
        "HOSTNAME", "HOST", "HOME", "PATH", "PWD", "OLDPWD", "SHLVL", "_", "TERM", "USER", "LOGNAME",
        "SHELL", "LANG", "TMPDIR", "TZ", "PORT", "DYNO", "INVOCATION_ID", "JOURNAL_STREAM",
    }
)
ENV_VAR_DENYLIST_PATTERNS = (
    re.compile(r"^LC_"),
    re.compile(r"^SYSTEMD_"),
    re.compile(r"^MEMORY_PRESSURE_"),
    re.compile(r"^KUBERNETES_"),
    re.compile(r"^FORGE_OPS_"),
    re.compile(r"_SERVICE_HOST$"),
    re.compile(r"_SERVICE_PORT"),
    re.compile(r"_PORT_.*_TCP"),
)


def normalize_kind(kind):
    kind = str(kind) if kind is not None else ""
    return kind if kind in KINDS else "other"


def build_change(configuration, kind, title, details=None, environment=None, service=None, actor=None, url=None, id=None, occurred_at=None):
    """The body for one record_change() call, or None when there's nothing sendable (a blank title:
    the server requires one, so posting it anyway would only ever come back 422)."""
    title = str(title).strip() if title is not None else ""
    if not title:
        return None

    payload = {
        "kind": normalize_kind(kind),
        "title": title[:MAX_TITLE_LENGTH],
        "details": details if isinstance(details, dict) else {},
        "environment": environment or configuration.environment,
        "service": service,
        "actor": actor,
        "url": url,
        "id": id,
        "occurred_at": _iso8601(occurred_at),
    }
    return {key: (value if key == "details" else str(value)) for key, value in payload.items() if value is not None}


def _iso8601(value):
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_denied_env_var(name):
    return name in ENV_VAR_DENYLIST or any(pattern.search(name) for pattern in ENV_VAR_DENYLIST_PATTERNS)


def env_var_names(environ=None):
    """Names only, never values, sorted so the same set always serializes the same way."""
    environ = os.environ if environ is None else environ
    return sorted(name for name in environ if not is_denied_env_var(name))[:MAX_ENV_VAR_NAMES]


def runtime():
    return f"python {platform.python_version()}"


def dependencies():
    """Every installed distribution this interpreter can import, as {name: version}, from
    importlib.metadata. When the same name is installed twice on sys.path, the first one wins, the
    same one an import would pick. None (left out) when nothing can be read."""
    try:
        from importlib import metadata

        found = {}
        for distribution in metadata.distributions():
            name = distribution.metadata["Name"]
            version = distribution.version
            if not name or not version:
                continue
            found.setdefault(str(name)[:MAX_NAME_LENGTH], str(version)[:MAX_VERSION_LENGTH])
        if not found:
            return None
        return dict(sorted(found.items())[:MAX_DEPENDENCIES])
    except Exception:  # noqa: BLE001 (a broken install must never fail the snapshot)
        return None


def build_snapshot(configuration):
    state = {"runtime": runtime(), "dependencies": dependencies()}
    if configuration.track_env_var_names:
        state["env_var_names"] = env_var_names()
    return {
        "environment": configuration.environment,
        "state": {key: value for key, value in state.items() if value is not None},
    }
