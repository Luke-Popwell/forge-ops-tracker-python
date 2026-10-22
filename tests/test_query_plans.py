import json
import queue
import threading
from datetime import datetime, timezone

import pytest

import forge_ops_tracker
from forge_ops_tracker import query_plans
from forge_ops_tracker.query_plans import (
    QueryPlanner,
    RateLimiter,
    build_payload,
    explainable,
    mask_plan,
)

SLOW_SELECT = "SELECT * FROM users WHERE email = %s AND id > 42"
PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "users",
            "Filter": "((email = 'a@b.co'::text) AND (id > 42))",
            "Total Cost": 35.5,
            "Plan Rows": 1,
        }
    }
]


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _init(monkeypatch, **overrides):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    settings = {"enabled_environments": {"production"}, "environment": "production", **overrides}
    configuration = forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events", **settings)
    return configuration, delivered


class _RecordingQueue:
    def __init__(self):
        self.payloads = []

    def push(self, payload):
        self.payloads.append(payload)
        return True


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# explainable: SELECT only, one statement, no locking clause, nothing data-modifying


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1",
        "  select * from users where id = %s",
        "SELECT * FROM users;",
        "-- a leading comment\nSELECT * FROM users",
        "/* app:web */ SELECT * FROM users",
        "SELECT * FROM users WHERE note = 'delete; update'",
        "SELECT * FROM users WHERE deleted_at IS NULL",
        "SELECT * FROM users -- trailing; comment",
    ],
)
def test_explainable_accepts_plain_selects(statement):
    assert explainable(statement)


@pytest.mark.parametrize(
    "statement",
    [
        None,
        "",
        "   ",
        "INSERT INTO users (email) VALUES (%s)",
        "UPDATE users SET email = %s",
        "DELETE FROM users",
        "WITH gone AS (DELETE FROM users RETURNING id) SELECT * FROM gone",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT * FROM users FOR UPDATE",
        "SELECT * FROM users for share",
        "SELECT * FROM users FOR NO KEY UPDATE",
        "SELECT * FROM users FOR KEY SHARE",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE users",
        "SELECT * FROM a MERGE",
        "EXPLAIN SELECT 1",
        "-- SELECT\nDELETE FROM users",
        "SELECT 1 -- it's fine\n; DELETE FROM users; --'",
    ],
)
def test_explainable_rejects_anything_that_is_not_a_single_plain_select(statement):
    assert not explainable(statement)


# Rate limits


def test_rate_limiter_allows_each_statement_once_per_ten_minutes():
    clock = _Clock()
    limiter = RateLimiter(clock)

    assert limiter.allow("SELECT ?")
    assert not limiter.allow("SELECT ?")
    clock.now += 599
    assert not limiter.allow("SELECT ?")
    clock.now += 1
    assert limiter.allow("SELECT ?")


def test_rate_limiter_allows_at_most_ten_per_minute_overall():
    clock = _Clock()
    limiter = RateLimiter(clock)

    assert all(limiter.allow(f"SELECT {i}") for i in range(10))
    assert not limiter.allow("SELECT 10")
    clock.now += 60
    assert limiter.allow("SELECT 10")


# Plan masking and size cap


def test_mask_plan_masks_every_string_and_leaves_numbers_keys_and_structure_alone():
    masked = mask_plan(PLAN)

    node = masked[0]["Plan"]
    assert node["Filter"] == "((email = ?::text) AND (id > ?))"
    assert node["Node Type"] == "Seq Scan"
    assert node["Relation Name"] == "users"
    assert node["Total Cost"] == 35.5
    assert node["Plan Rows"] == 1
    assert "a@b.co" not in json.dumps(masked)


def test_build_payload_parses_a_plan_returned_as_json_text(monkeypatch):
    configuration, _ = _init(monkeypatch)
    payload = build_payload(configuration, "SELECT ?", "postgresql", json.dumps(PLAN), 812.5, datetime(2026, 9, 25, tzinfo=timezone.utc))

    assert payload["plan"][0]["Plan"]["Filter"] == "((email = ?::text) AND (id > ?))"
    assert payload["captured_at"] == "2026-09-25T00:00:00.000Z"
    assert payload["environment"] == "production"


def test_build_payload_drops_a_plan_over_64_kb(monkeypatch):
    configuration, _ = _init(monkeypatch)
    huge = [{"Plan": {"Node Type": "Seq Scan", "Output": ["x" * 1000] * 70}}]
    assert build_payload(configuration, "SELECT ?", "postgresql", huge, 900, datetime.now(timezone.utc)) is None


# QueryPlanner gating


def _planner(configuration, clock=None):
    delivery = _RecordingQueue()
    return QueryPlanner(configuration, delivery, clock or _Clock()), delivery


def test_explain_is_off_by_default(monkeypatch):
    configuration, _ = _init(monkeypatch)
    planner, _ = _planner(configuration)
    calls = []

    assert configuration.explain_slow_queries is False
    assert configuration.explain_threshold_ms == 500
    assert not planner.consider(SLOW_SELECT, "postgresql", 5000, lambda: calls.append(1))
    assert calls == []


def test_explain_does_nothing_under_strict_privacy(monkeypatch):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True, strict_privacy=True)
    planner, _ = _planner(configuration)
    calls = []

    assert not planner.consider(SLOW_SELECT, "postgresql", 5000, lambda: calls.append(1))
    assert calls == []


def test_explain_skips_queries_under_the_threshold_other_databases_and_non_selects(monkeypatch):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True, explain_threshold_ms=500)
    planner, _ = _planner(configuration)

    assert not planner.consider(SLOW_SELECT, "postgresql", 499.9, lambda: PLAN)
    assert not planner.consider(SLOW_SELECT, "mysql", 5000, lambda: PLAN)
    assert not planner.consider(SLOW_SELECT, None, 5000, lambda: PLAN)
    assert not planner.consider("UPDATE users SET email = %s", "postgresql", 5000, lambda: PLAN)
    assert not planner.consider("SELECT * FROM users FOR UPDATE", "postgresql", 5000, lambda: PLAN)
    assert planner.consider(SLOW_SELECT, "postgresql", 500, lambda: PLAN)


def test_explain_is_skipped_when_the_client_is_not_enabled(monkeypatch):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True, environment="development")
    planner, _ = _planner(configuration)

    assert not planner.consider(SLOW_SELECT, "postgresql", 5000, lambda: PLAN)


def test_explain_rate_limits_repeats_of_the_same_masked_statement(monkeypatch):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True)
    clock = _Clock()
    planner, _ = _planner(configuration, clock)
    monkeypatch.setattr(planner, "_ensure_worker_started", lambda: None)

    assert planner.consider("SELECT * FROM users WHERE id = 1", "postgresql", 900, lambda: PLAN)
    # Different literal, same masked statement.
    assert not planner.consider("SELECT * FROM users WHERE id = 2", "postgresql", 900, lambda: PLAN)
    clock.now += 600
    assert planner.consider("SELECT * FROM users WHERE id = 3", "postgresql", 900, lambda: PLAN)


def test_explain_runs_on_the_planners_own_thread_and_delivers_only_masked_data(monkeypatch):
    _, delivered = _init(monkeypatch, explain_slow_queries=True)
    ran_on = queue.Queue()

    def explain():
        ran_on.put(threading.current_thread())
        return PLAN

    _, planner = forge_ops_tracker._query_planner_state()
    assert planner.consider("SELECT * FROM users WHERE email = 'a@b.co' AND id > 42", "postgresql", 812.5, explain)

    assert ran_on.get(timeout=2) is not threading.current_thread()
    request = delivered.get(timeout=2)
    assert request.full_url == "https://tracker.example.com/api/v1/query_plans"
    assert request.get_header("Authorization") == "Bearer key"
    body = request.data.decode("utf-8")
    assert "a@b.co" not in body
    payload = json.loads(body)
    # Only the fields that carry the statement and the plan: captured_at is a real clock reading,
    # which contains "42" at the 42nd minute or second of any hour.
    assert "42" not in json.dumps([payload["statement"], payload["plan"]])
    assert payload["statement"] == "SELECT * FROM users WHERE email = ? AND id > ?"
    assert payload["db_system"] == "postgresql"
    assert payload["duration_ms"] == 812.5
    assert payload["environment"] == "production"
    assert payload["plan"][0]["Plan"]["Filter"] == "((email = ?::text) AND (id > ?))"
    assert payload["captured_at"].endswith("Z")


def test_explain_failures_are_swallowed_and_logged_at_debug(monkeypatch, caplog):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True)
    planner, delivery = _planner(configuration)

    def explain():
        raise TimeoutError("canceling statement due to statement timeout")

    job = (explain, "SELECT ?", "postgresql", 900, datetime.now(timezone.utc))
    with caplog.at_level("DEBUG", logger="forge_ops_tracker"):
        assert planner.run_job(job) is False
    assert delivery.payloads == []
    assert "EXPLAIN failed" in caplog.text
    assert not query_plans.is_explaining()


def test_an_unparseable_plan_is_dropped(monkeypatch):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True)
    planner, delivery = _planner(configuration)
    assert planner.run_job((lambda: "not json", "SELECT ?", "postgresql", 900, datetime.now(timezone.utc))) is False
    assert planner.run_job((lambda: None, "SELECT ?", "postgresql", 900, datetime.now(timezone.utc))) is False
    assert delivery.payloads == []


def test_is_explaining_is_only_true_while_the_explain_runs(monkeypatch):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True)
    planner, delivery = _planner(configuration)
    seen = []
    def explain():
        seen.append(query_plans.is_explaining())
        return PLAN

    assert planner.run_job((explain, "SELECT ?", "postgresql", 900, datetime.now(timezone.utc)))
    assert seen == [True]
    assert not query_plans.is_explaining()
    assert len(delivery.payloads) == 1


def test_a_full_pending_queue_drops_the_explain_without_raising(monkeypatch):
    configuration, _ = _init(monkeypatch, explain_slow_queries=True)
    planner, _ = _planner(configuration)
    monkeypatch.setattr(planner, "_ensure_worker_started", lambda: None)
    planner._pending = queue.Queue(maxsize=1)

    assert planner.consider("SELECT * FROM a", "postgresql", 900, lambda: PLAN)
    assert not planner.consider("SELECT * FROM b", "postgresql", 900, lambda: PLAN)


# Database spans: masked statement and db.system


def test_record_database_query_puts_the_masked_statement_and_db_system_on_the_span(monkeypatch):
    _init(monkeypatch)
    token = forge_ops_tracker._start_span_trace()
    forge_ops_tracker._record_database_query(
        "SELECT users", datetime.now(timezone.utc), 3.0, "SELECT * FROM users WHERE email = 'a@b.co' AND id = 42", db_system="postgresql"
    )
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    span = buffer.spans[0]
    assert span["kind"] == "database"
    assert span["name"] == "SELECT users"
    assert span["data"] == {"db.statement": "SELECT * FROM users WHERE email = ? AND id = ?", "db.system": "postgresql"}
    assert "a@b.co" not in json.dumps(buffer.spans)


def test_record_database_query_truncates_the_statement_to_4000_characters(monkeypatch):
    _init(monkeypatch)
    token = forge_ops_tracker._start_span_trace()
    forge_ops_tracker._record_database_query("SELECT", datetime.now(timezone.utc), 1.0, "SELECT " + "a, " * 3000 + "b")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert len(buffer.spans[0]["data"]["db.statement"]) == 4000
    assert "db.system" not in buffer.spans[0]["data"]


def test_record_database_query_never_raises_when_the_planner_blows_up(monkeypatch):
    _init(monkeypatch, explain_slow_queries=True)
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(forge_ops_tracker, "_query_planner_state", broken)
    forge_ops_tracker._record_database_query("SELECT", datetime.now(timezone.utc), 900, SLOW_SELECT, "postgresql", explain=lambda: PLAN)


def test_manual_database_span_takes_a_statement_and_masks_it(monkeypatch):
    _init(monkeypatch)

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("Load orders", kind="database", statement="SELECT * FROM orders WHERE total > 100.50", db_system="PostgreSQL"):
        pass
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    span = buffer.spans[0]
    assert span["kind"] == "database"
    assert span["data"] == {"db.statement": "SELECT * FROM orders WHERE total > ?", "db.system": "postgresql"}


def test_a_mysql_statement_has_its_double_quoted_strings_masked_on_every_span(monkeypatch):
    _init(monkeypatch)
    statement = 'SELECT * FROM sessions WHERE token = "abc123secret"'

    token = forge_ops_tracker._start_span_trace()
    forge_ops_tracker._record_database_query("SELECT sessions", datetime.now(timezone.utc), 3.0, statement, db_system="mysql")
    with forge_ops_tracker.span("Load session", kind="database", statement=statement, db_system="MariaDB"):
        pass
    forge_ops_tracker._record_database_query("SELECT sessions", datetime.now(timezone.utc), 3.0, statement, db_system="postgresql")
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    statements = [s["data"]["db.statement"] for s in buffer.spans]
    assert statements == [
        "SELECT * FROM sessions WHERE token = ?",
        "SELECT * FROM sessions WHERE token = ?",
        statement,  # on PostgreSQL a "double quoted" name is an identifier, not a value
    ]


def test_manual_span_keeps_its_own_data_alongside_the_statement(monkeypatch):
    _init(monkeypatch)

    token = forge_ops_tracker._start_span_trace()
    with forge_ops_tracker.span("Load orders", kind="database", data={"rows": 3}, statement="SELECT 1"):
        pass
    buffer = forge_ops_tracker._spans.get()
    forge_ops_tracker._end_span_trace(token)

    assert buffer.spans[0]["data"] == {"rows": 3, "db.statement": "SELECT ?"}
