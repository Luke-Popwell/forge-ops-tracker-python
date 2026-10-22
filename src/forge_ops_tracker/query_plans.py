"""Opt-in EXPLAIN for slow PostgreSQL queries (configuration.explain_slow_queries, off by default).

When a query the Django or SQLAlchemy integration timed took at least explain_threshold_ms, and
it's a plain read (see explainable below), this runs EXPLAIN (FORMAT JSON) for it on a background
thread, on a separate connection, inside a READ ONLY transaction with a 2 second
statement_timeout, and then rolls that transaction back. Never EXPLAIN ANALYZE, so the query itself
is never run again. The original statement and its bind values stay in memory only until that
EXPLAIN runs; what gets sent to POST /api/v1/query_plans is the masked statement (exactly what the
query's span carries) and the plan with every string in it masked the same way.

Rate limited per process: each distinct masked statement at most once per 10 minutes, and at most
10 EXPLAINs per minute overall. Any failure (a timeout, a permission error, a pool with no free
connection) is logged at debug and dropped; nothing here ever raises into the app.
"""

import json
import queue
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import sql_statement

STATEMENT_TIMEOUT = "2s"
PER_STATEMENT_INTERVAL = 600.0  # seconds
MAX_PER_MINUTE = 10
MAX_PLAN_BYTES = 64 * 1024
# Pending EXPLAINs waiting for the worker. The rate limits already keep this tiny; a full queue
# just drops the newest one.
MAX_PENDING = 10

# One pass over the statement, leftmost match wins, so a quote inside a comment or a comment marker
# inside a string can't hide anything: string literals (including dollar-quoted ones) become "?",
# line and block comments become a space.
_LITERALS_AND_COMMENTS = re.compile(
    r"""
    '(?:[^']|'')*(?:'|\Z)
    | (?P<tag>\$[A-Za-z_]*\$).*?(?:(?P=tag)|\Z)
    | (?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
    """,
    re.DOTALL | re.VERBOSE,
)
_STARTS_WITH_SELECT = re.compile(r"\A\s*SELECT\b", re.IGNORECASE)
_LOCKING_CLAUSE = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|UPDATE|KEY\s+SHARE|SHARE)\b", re.IGNORECASE)
_DATA_MODIFYING = re.compile(r"\b(?:INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)

_DB_SYSTEM_ALIASES = {"postgres": "postgresql", "microsoft": "mssql", "sqlserver": "mssql"}

_explaining = threading.local()


def db_system_name(name):
    """The lowercase db.system for a Django connection vendor or SQLAlchemy dialect name
    ("postgresql", "mysql", "sqlite", "oracle", "mssql", ...), or None when unknown."""
    if not name or not isinstance(name, str):
        return None
    name = name.lower()
    return _DB_SYSTEM_ALIASES.get(name, name)


def is_explaining():
    """True on the worker thread while it runs an EXPLAIN, so the SQLAlchemy integration's own
    listeners don't time or record the EXPLAIN's statements as app queries."""
    return getattr(_explaining, "active", False)


def explainable(statement):
    """Whether a statement is safe to EXPLAIN: after comments and leading whitespace, it starts
    with SELECT, has no second statement (no ";" except one at the very end), no locking clause
    (FOR UPDATE, FOR SHARE, FOR NO KEY UPDATE, FOR KEY SHARE), and no INSERT, UPDATE, DELETE or
    MERGE anywhere (a data-modifying CTE)."""
    if not isinstance(statement, str) or not statement.strip():
        return False
    sql = _LITERALS_AND_COMMENTS.sub(lambda m: " " if m.group("comment") else "?", statement)
    if not _STARTS_WITH_SELECT.match(sql):
        return False
    if ";" in sql.rstrip().removesuffix(";"):
        return False
    return not (_LOCKING_CLAUSE.search(sql) or _DATA_MODIFYING.search(sql))


def mask_plan(value):
    """Every string in the plan masked with the same masker the statement goes through (a plan's
    "Filter" or "Index Cond" repeats the query's literals); numbers, keys and structure unchanged."""
    if isinstance(value, str):
        masked = sql_statement.mask(value)
        return value if masked is None else masked
    if isinstance(value, list):
        return [mask_plan(item) for item in value]
    if isinstance(value, dict):
        return {key: mask_plan(item) for key, item in value.items()}
    return value


def build_payload(configuration, masked_statement, db_system, plan, duration_ms, captured_at):
    """The POST /api/v1/query_plans body, or None when the plan isn't usable or its serialized form
    is over MAX_PLAN_BYTES."""
    if isinstance(plan, (bytes, bytearray)):
        plan = plan.decode("utf-8")
    if isinstance(plan, str):
        plan = json.loads(plan)
    if not isinstance(plan, list):
        return None
    plan = mask_plan(plan)
    if len(json.dumps(plan).encode("utf-8")) > MAX_PLAN_BYTES:
        return None
    return {
        "statement": masked_statement,
        "db_system": db_system,
        "plan": plan,
        "duration_ms": duration_ms,
        "environment": configuration.environment,
        "captured_at": _iso8601_millis(captured_at),
    }


class RateLimiter:
    """Each distinct key at most once per PER_STATEMENT_INTERVAL, and at most MAX_PER_MINUTE
    allowed per rolling minute overall. Thread-safe; clock is injectable for tests."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen = {}
        self._recent = deque()

    def allow(self, key):
        with self._lock:
            now = self._clock()
            last = self._last_seen.get(key)
            if last is not None and now - last < PER_STATEMENT_INTERVAL:
                return False
            while self._recent and now - self._recent[0] >= 60.0:
                self._recent.popleft()
            if len(self._recent) >= MAX_PER_MINUTE:
                return False
            self._recent.append(now)
            self._last_seen[key] = now
            if len(self._last_seen) > 1000:
                self._last_seen = {k: t for k, t in self._last_seen.items() if now - t < PER_STATEMENT_INTERVAL}
            return True


class QueryPlanner:
    """Decides whether a timed query gets an EXPLAIN and, if so, runs it on this planner's own
    daemon thread (started lazily on first use, the same fork-safety reasoning DeliveryQueue's
    module doc gives), then hands the payload to delivery_queue, the SDK's normal async delivery."""

    def __init__(self, configuration, delivery_queue, clock=time.monotonic):
        self._configuration = configuration
        self._delivery_queue = delivery_queue
        self._limiter = RateLimiter(clock)
        self._pending = queue.Queue(maxsize=MAX_PENDING)
        self._thread = None
        self._start_lock = threading.Lock()

    def consider(self, statement, db_system, duration_ms, explain, masked=None):
        """Called on the app's own thread right after a query finished. explain is a callable taking
        no arguments that runs EXPLAIN (FORMAT JSON) on a separate connection and returns the plan;
        it's only ever called on the worker thread. Returns whether an EXPLAIN was queued. Never
        raises."""
        try:
            configuration = self._configuration
            # Strict privacy sends no SQL at all, masked or not (sdks/STRICT_PRIVACY.md).
            if not (configuration.explain_slow_queries and configuration.is_enabled()) or configuration.strict_privacy:
                return False
            if db_system != "postgresql" or duration_ms < configuration.explain_threshold_ms:
                return False
            if not explainable(statement):
                return False
            masked = masked or sql_statement.mask_for_span(statement, db_system)
            if masked is None or not self._limiter.allow(masked):
                return False
            job = (explain, masked, db_system, duration_ms, datetime.now(timezone.utc))
            self._ensure_worker_started()
            self._pending.put_nowait(job)
            return True
        except queue.Full:
            self._configuration.logger.debug("[forge_ops_tracker] query plan queue full, skipping EXPLAIN")
            return False
        except Exception as e:  # noqa: BLE001 (this runs on the app's own thread and must never raise into it)
            self._configuration.logger.debug("[forge_ops_tracker] query plan skipped: %s: %s", type(e).__name__, e)
            return False

    def _ensure_worker_started(self):
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            self.run_job(self._pending.get())

    def run_job(self, job):
        """Runs one queued EXPLAIN and delivers its plan. Public only so tests can run a job
        synchronously; returns whether a payload was queued for delivery."""
        explain, masked, db_system, duration_ms, captured_at = job
        _explaining.active = True
        try:
            plan = explain()
            payload = build_payload(self._configuration, masked, db_system, plan, duration_ms, captured_at)
        except Exception as e:  # noqa: BLE001 (a failed EXPLAIN is dropped, never raised)
            self._configuration.logger.debug("[forge_ops_tracker] EXPLAIN failed: %s: %s", type(e).__name__, e)
            return False
        finally:
            _explaining.active = False
        if payload is None:
            self._configuration.logger.debug("[forge_ops_tracker] query plan dropped: unusable or over %d bytes", MAX_PLAN_BYTES)
            return False
        return self._delivery_queue.push(payload)


def _iso8601_millis(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
