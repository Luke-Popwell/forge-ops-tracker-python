"""SQLAlchemy query timing. Call once, after creating your engine:

    from forge_ops_tracker.integrations.sqlalchemy import track_sqlalchemy_queries
    track_sqlalchemy_queries(engine)

Or, with Flask-SQLAlchemy:

    db = SQLAlchemy(app)
    track_sqlalchemy_queries(db.engine)

Not imported by forge_ops_tracker/__init__.py, and not part of init_flask() either: unlike Django
(whose ORM is built in), a Flask app might use SQLAlchemy, a different database layer entirely, or
no database at all, so this is its own explicit opt-in, named for what it actually depends on
rather than folded into the Flask integration. Genuinely usable outside Flask too, for any Python
app using SQLAlchemy directly.

Each query's database span carries "db.statement" (masked: every string and number becomes "?")
and "db.system" (the engine's dialect name). With explain_slow_queries on, a slow SELECT on a
PostgreSQL engine also gets an EXPLAIN; see query_plans.py and _explain_on_engine below.
"""

import time
from datetime import datetime, timezone

from sqlalchemy import event

from .. import (
    _query_transaction_name,
    _record_breadcrumb,
    _record_database_query,
    _record_performance,
)
from ..query_plans import STATEMENT_TIMEOUT, db_system_name, is_explaining


def track_sqlalchemy_queries(engine):
    # event.listen(Engine, ...) (a bare class, not a specific engine) would install this for
    # every engine in the whole process; listening on this one engine instance instead keeps the
    # blast radius to exactly what the caller passed in, the same "opt into exactly what you
    # asked for" reasoning init_flask itself already follows for Flask's own hooks.
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(engine, "after_cursor_execute", _after_cursor_execute)


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if is_explaining():
        return  # the planner's own EXPLAIN on its own connection, not an app query
    # conn.info is SQLAlchemy's own documented per-connection scratch space for exactly this kind
    # of bookkeeping, not a thread-local: a connection is only ever in use by one query at a time,
    # so there's no concurrent-access concern a thread-local would otherwise exist to solve, and
    # this reads back correctly regardless of which thread eventually runs after_cursor_execute
    # for the same connection. Stashes both a monotonic start (for an exact duration) and a wall-
    # clock start (for the span's own started_at, only ever needed if this query turns out to be
    # recorded as one below).
    conn.info.setdefault("forge_ops_tracker_query_start", []).append((time.monotonic(), datetime.now(timezone.utc)))


def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if is_explaining():
        return
    starts = conn.info.get("forge_ops_tracker_query_start")
    if not starts:
        return

    start, started_at = starts.pop()
    duration_ms = (time.monotonic() - start) * 1000
    transaction_name = _query_transaction_name(statement)
    _record_performance(transaction_name, duration_ms, kind="query")
    # Independent of track_performance above, same reasoning every other automatic breadcrumb
    # source in this SDK documents: records into whatever trail is currently active (a Flask/
    # Django request's own, if this engine is queried from inside one; a lazily-created
    # standalone one otherwise), never this integration's own concern to set up or scope that
    # trail itself.
    _record_breadcrumb(transaction_name, category="query", data={"duration_ms": round(duration_ms, 1)})
    # A leaf span, parented onto whatever's currently open (a Flask request's own root, or a
    # manually-wrapped forge_ops_tracker.span() block around it): a query has no children of its
    # own to nest anything under. A no-op with no trace currently being captured, the same
    # "extend an existing instrumentation point, never this integration's own concern to set up
    # tracing itself" reasoning the breadcrumb call just above already follows.
    # The statement is masked before it goes on the span; the raw statement and parameters are only
    # captured by the explain closure, held in memory until the planner's thread runs it.
    db_system = db_system_name(getattr(conn.dialect, "name", None))
    explain = None
    if not executemany and db_system == "postgresql":
        engine = conn.engine

        def explain():
            return _explain_on_engine(engine, statement, parameters)

    _record_database_query(transaction_name, started_at, duration_ms, statement, db_system=db_system, explain=explain)


def _explain_on_engine(engine, statement, parameters):
    """Runs on the query planner's own thread, never the app's: checks out a separate connection
    from the engine's pool (the app's own connection is never touched, and none of its transaction
    state carries over), makes the new transaction READ ONLY with a statement_timeout, runs EXPLAIN
    (FORMAT JSON) with the original statement and parameters, and always rolls back."""
    with engine.connect() as conn:
        try:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            row = conn.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters or None).fetchone()
        finally:
            conn.rollback()
    return row[0] if row else None
