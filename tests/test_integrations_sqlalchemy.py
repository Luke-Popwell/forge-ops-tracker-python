import asyncio
import json
import queue

import pytest
from sqlalchemy import create_engine, text

import forge_ops_tracker
from forge_ops_tracker.integrations.sqlalchemy import (
    _after_cursor_execute,
    _before_cursor_execute,
    _explain_on_engine,
    track_sqlalchemy_queries,
)


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_times_a_real_query_run_through_a_tracked_engine(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    forge_ops_tracker._performance_state()[1].flush()

    request = delivered.get(timeout=1)
    payload = json.loads(request.data)
    sample = payload["samples"][0]
    assert sample["kind"] == "query"
    assert sample["transaction_name"] == "SELECT"
    assert sample["request_count"] == 1


def test_records_a_query_breadcrumb_on_whatever_trail_is_currently_active(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    token = forge_ops_tracker._start_breadcrumb_trail()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    buffer = forge_ops_tracker._breadcrumbs.get()
    forge_ops_tracker._end_breadcrumb_trail(token)

    entry = buffer.all()[0]
    assert entry["category"] == "query"
    assert entry["message"] == "SELECT"
    assert "duration_ms" in entry["data"]


def test_query_breadcrumb_lazily_creates_a_standalone_trail_with_no_request_or_task_active(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    buffer = forge_ops_tracker._breadcrumbs.get()
    assert buffer is not None
    assert buffer.all()[0]["message"] == "SELECT"


def test_does_not_record_a_query_breadcrumb_when_track_breadcrumbs_is_off(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        track_breadcrumbs=False,
    )

    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    assert forge_ops_tracker._breadcrumbs.get() is None


def test_records_a_database_span_on_whatever_trace_is_currently_active(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    token = forge_ops_tracker._start_span_trace()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert len(buffer.spans) == 1
    span = buffer.spans[0]
    assert span["kind"] == "database"
    assert span["name"] == "SELECT"
    assert span["parent_span_id"] == buffer.root_span_id


def test_queries_nest_under_nested_spans_and_concurrent_sibling_spans_each_keep_their_own(monkeypatch):
    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", lambda request, timeout=None: _FakeResponse(202))
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    def query(sql):
        with engine.connect() as conn:
            conn.execute(text(sql))

    async def part(name, sql):
        with forge_ops_tracker.span(name):
            await asyncio.sleep(0)  # let the sibling open its own span in between
            await asyncio.to_thread(query, sql)

    async def run():
        token = forge_ops_tracker._start_span_trace()
        try:
            with forge_ops_tracker.span("OrderService.load"), forge_ops_tracker.span("OrderService.lines"):
                query("SELECT 1")
            await asyncio.gather(part("A", "SELECT 2"), part("B", "SELECT 2, 3"))
            return forge_ops_tracker._spans.get()
        finally:
            forge_ops_tracker._end_span_trace(token)

    buffer = asyncio.run(run())
    spans = {s["name"]: s for s in buffer.spans if s["kind"] != "database"}
    queries = [s for s in buffer.spans if s["kind"] == "database"]

    assert spans["OrderService.lines"]["parent_span_id"] == spans["OrderService.load"]["span_id"]
    assert queries[0]["parent_span_id"] == spans["OrderService.lines"]["span_id"]
    assert spans["A"]["parent_span_id"] == buffer.root_span_id
    assert spans["B"]["parent_span_id"] == buffer.root_span_id
    by_statement = {q["data"]["db.statement"]: q for q in queries[1:]}
    assert by_statement["SELECT ?"]["parent_span_id"] == spans["A"]["span_id"]
    assert by_statement["SELECT ?, ?"]["parent_span_id"] == spans["B"]["span_id"]


def test_does_not_record_a_database_span_with_no_trace_currently_active(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    assert forge_ops_tracker._spans.get() is None


def test_only_times_queries_on_the_engine_it_was_told_to_track(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    tracked_engine = create_engine("sqlite:///:memory:")
    untracked_engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(tracked_engine)

    with untracked_engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    forge_ops_tracker._performance_state()[1].flush()
    assert delivered.empty()


# Masked SQL on database spans, and the opt-in EXPLAIN on PostgreSQL


def _init_capturing(monkeypatch, **overrides):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    settings = {"enabled_environments": {"production"}, "environment": "production", **overrides}
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events", **settings)
    return delivered


def test_database_span_carries_the_masked_statement_and_db_system(monkeypatch):
    _init_capturing(monkeypatch)
    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    token = forge_ops_tracker._start_span_trace()
    with engine.connect() as conn:
        conn.execute(text("SELECT 'secret@example.com' AS email, 42 AS answer"))
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    data = buffer.spans[0]["data"]
    assert data == {"db.statement": "SELECT ? AS email, ? AS answer", "db.system": "sqlite"}
    assert "secret@example.com" not in json.dumps(buffer.spans)


def test_never_explains_a_non_postgres_engine_even_when_enabled(monkeypatch):
    _init_capturing(monkeypatch, explain_slow_queries=True, explain_threshold_ms=0)
    considered = []
    monkeypatch.setattr(forge_ops_tracker, "_query_planner_state", lambda: considered.append(1))
    engine = create_engine("sqlite:///:memory:")
    track_sqlalchemy_queries(engine)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    assert considered == []


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Stands in for both the app's own Connection (handed to the listener) and the one the
    planner checks out with engine.connect()."""

    def __init__(self, engine, plan=None, fail_on=None):
        self.engine = engine
        self.dialect = engine.dialect
        self.info = {}
        self.executed = []
        self.rolled_back = False
        self.closed = False
        self._plan = plan
        self._fail_on = fail_on

    def exec_driver_sql(self, statement, parameters=None):
        self.executed.append((statement, parameters))
        if self._fail_on and self._fail_on in statement:
            raise RuntimeError("permission denied")
        return _FakeResult([self._plan] if statement.startswith("EXPLAIN") else None)

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False


class _FakeDialect:
    name = "postgresql"


class _FakeEngine:
    def __init__(self, plan=None, fail_on=None):
        self.dialect = _FakeDialect()
        self.checked_out = []
        self._plan = plan
        self._fail_on = fail_on

    def connect(self):
        conn = _FakeConnection(self, self._plan, self._fail_on)
        self.checked_out.append(conn)
        return conn


PLAN = [{"Plan": {"Node Type": "Index Scan", "Index Cond": "(email = 'a@b.co'::text)", "Total Cost": 8.3}}]


def test_explain_on_engine_uses_a_fresh_read_only_transaction_with_a_timeout_and_rolls_back():
    engine = _FakeEngine(plan=PLAN)
    plan = _explain_on_engine(engine, "SELECT * FROM users WHERE email = %(email)s", {"email": "a@b.co"})

    assert plan == PLAN
    conn = engine.checked_out[0]
    assert conn.executed == [
        ("SET TRANSACTION READ ONLY", None),
        ("SET LOCAL statement_timeout = '2s'", None),
        ("EXPLAIN (FORMAT JSON) SELECT * FROM users WHERE email = %(email)s", {"email": "a@b.co"}),
    ]
    assert conn.rolled_back
    assert conn.closed
    assert not any("ANALYZE" in statement for statement, _ in conn.executed)


def test_explain_on_engine_still_rolls_back_and_releases_the_connection_when_it_fails():
    engine = _FakeEngine(fail_on="EXPLAIN")
    with pytest.raises(RuntimeError):
        _explain_on_engine(engine, "SELECT 1", ())
    conn = engine.checked_out[0]
    assert conn.rolled_back
    assert conn.closed


def test_slow_postgres_select_is_explained_on_a_separate_connection_and_only_masked_data_is_sent(monkeypatch):
    delivered = _init_capturing(monkeypatch, explain_slow_queries=True, explain_threshold_ms=0)
    engine = _FakeEngine(plan=PLAN)
    app_conn = _FakeConnection(engine)
    statement = "SELECT * FROM users WHERE email = %(email)s"
    parameters = {"email": "a@b.co"}

    _before_cursor_execute(app_conn, None, statement, parameters, None, False)
    _after_cursor_execute(app_conn, None, statement, parameters, None, False)

    request = delivered.get(timeout=2)
    assert request.full_url == "https://tracker.example.com/api/v1/query_plans"
    body = request.data.decode("utf-8")
    assert "a@b.co" not in body
    payload = json.loads(body)
    assert payload["statement"] == statement
    assert payload["db_system"] == "postgresql"
    assert payload["plan"][0]["Plan"]["Index Cond"] == "(email = ?::text)"

    # The app's own connection never ran anything; the EXPLAIN went through a checked-out one.
    assert app_conn.executed == []
    assert len(engine.checked_out) == 1
    assert engine.checked_out[0].executed[2] == (f"EXPLAIN (FORMAT JSON) {statement}", parameters)


def test_executemany_and_writes_are_never_explained(monkeypatch):
    _init_capturing(monkeypatch, explain_slow_queries=True, explain_threshold_ms=0)
    considered = []

    class _Planner:
        def consider(self, *args, **kwargs):
            considered.append(args)

    monkeypatch.setattr(forge_ops_tracker, "_query_planner_state", lambda: (None, _Planner()))
    engine = _FakeEngine(plan=PLAN)
    app_conn = _FakeConnection(engine)

    _before_cursor_execute(app_conn, None, "SELECT * FROM users WHERE id = %s", [("x",)], None, True)
    _after_cursor_execute(app_conn, None, "SELECT * FROM users WHERE id = %s", [("x",)], None, True)
    assert considered == []

    # A write still reaches the planner (which rejects it; see test_query_plans.py), never the engine.
    _before_cursor_execute(app_conn, None, "UPDATE users SET name = %s", ("x",), None, False)
    _after_cursor_execute(app_conn, None, "UPDATE users SET name = %s", ("x",), None, False)
    assert engine.checked_out == []


def test_listeners_ignore_the_planners_own_explain_statements(monkeypatch):
    _init_capturing(monkeypatch)
    engine = _FakeEngine()
    conn = _FakeConnection(engine)
    monkeypatch.setattr("forge_ops_tracker.integrations.sqlalchemy.is_explaining", lambda: True)

    token = forge_ops_tracker._start_span_trace()
    _before_cursor_execute(conn, None, "EXPLAIN (FORMAT JSON) SELECT 1", None, None, False)
    _after_cursor_execute(conn, None, "EXPLAIN (FORMAT JSON) SELECT 1", None, None, False)
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert conn.info == {}
    assert buffer.spans == []
