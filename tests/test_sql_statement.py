import pytest

from forge_ops_tracker import sql_statement
from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.event_builder import EventBuilder


class FakeDbError(Exception):
    def __init__(self, message, statement):
        super().__init__(message)
        self.statement = statement


def test_find_in_reads_statement_off_the_error():
    assert sql_statement.find_in(FakeDbError("boom", "SELECT 1")) == "SELECT 1"


def test_find_in_walks_the_cause_chain():
    try:
        try:
            raise FakeDbError("db", "CALL refund_order(1)")
        except FakeDbError as inner:
            raise ValueError("refund failed") from inner
    except ValueError as outer:
        assert sql_statement.find_in(outer) == "CALL refund_order(1)"


def test_find_in_returns_none_without_sql():
    assert sql_statement.find_in(ValueError("nope")) is None


def test_mask_replaces_strings_and_numbers_but_not_identifiers_or_placeholders():
    assert (
        sql_statement.mask("SELECT * FROM orders2 WHERE email = 'a@b.co' AND id = 42 AND x = $1")
        == "SELECT * FROM orders2 WHERE email = ? AND id = ? AND x = $1"
    )


def test_mask_handles_escaped_quote_cut_off_string_and_dollar_quoted_body():
    assert sql_statement.mask("EXEC sp_x @t = 'it''s'") == "EXEC sp_x @t = ?"
    assert sql_statement.mask("SELECT 1 WHERE n = 'oops") == "SELECT ? WHERE n = ?"
    assert sql_statement.mask("DO $b$ BEGIN PERFORM 1; END $b$") == "DO ?"


def test_mask_is_idempotent_and_truncates():
    once = sql_statement.mask("SELECT * FROM t WHERE a = 'x' AND b = 9")
    assert sql_statement.mask(once) == once
    assert len(sql_statement.mask("SELECT " + "a, " * 3000 + "b")) == sql_statement.MAX_LENGTH + 3


# The server's own masking corpus (app/services/sql_statement_masker.rb's test set), case for
# case: (statement, db.system, what the server stores). The SDK must produce exactly the same.
SERVER_CORPUS = [
    ("SELECT * FROM orders WHERE email = 'a@b.co' AND id = 42 LIMIT 10", None, 'SELECT * FROM orders WHERE email = ? AND id = ? LIMIT ?'),
    ("EXEC sp_note @text = 'it''s broken'", None, 'EXEC sp_note @text = ?'),
    ("SELECT 1 WHERE name = 'unterminated", None, 'SELECT ? WHERE name = ?'),
    ('DO $body$ BEGIN PERFORM 1; END $body$', None, 'DO ?'),
    ('SELECT "user id" FROM orders2 WHERE id = $1 AND v = sp_v2(?)', None, 'SELECT "user id" FROM orders2 WHERE id = $1 AND v = sp_v2(?)'),
    ('SELECT price * 1.5 FROM t', None, 'SELECT price * ? FROM t'),
    ("SELECT * FROM users WHERE name = E'o\\'brien' AND id = 1", None, 'SELECT * FROM users WHERE name = ? AND id = ?'),
    ("SELECT * FROM users WHERE name = 'o\\'brien' AND id = 1", None, 'SELECT * FROM users WHERE name = ? AND id = ?'),
    ("SELECT * FROM t WHERE b = X'DEADBEEF' AND s = N'uni' AND u = U&'d\\0061t' AND e = e'x'", None, 'SELECT * FROM t WHERE b = ? AND s = ? AND u = ? AND e = ?'),
    ("SELECT * FROM t WHERE a LIKE'%secret%'", None, 'SELECT * FROM t WHERE a LIKE?'),
    ('SELECT * FROM t WHERE f = 0x1F AND b = 0b101 AND n = 3e10 AND m = 1.5E-3 AND k = .5', None, 'SELECT * FROM t WHERE f = ? AND b = ? AND n = ? AND m = ? AND k = ?'),
    ('SELECT e, t.col, 1e5e FROM t', None, 'SELECT e, t.col, 1e5e FROM t'),
    ('SELECT "user id" FROM t WHERE token = "abc123secret"', 'mysql', 'SELECT ? FROM t WHERE token = ?'),
    ('SELECT "user id" FROM t WHERE token = "abc123secret"', 'MariaDB', 'SELECT ? FROM t WHERE token = ?'),
    ('SELECT "user id" FROM t WHERE token = "abc123secret"', 'postgresql', 'SELECT "user id" FROM t WHERE token = "abc123secret"'),
    ('SELECT "user id" FROM t WHERE token = "abc123secret"', None, 'SELECT "user id" FROM t WHERE token = "abc123secret"'),
    ("SELECT * FROM t WHERE a = 'x' AND b = 9", None, 'SELECT * FROM t WHERE a = ? AND b = ?'),
    ('SELECT * FROM t WHERE a = ? AND b = ?', None, 'SELECT * FROM t WHERE a = ? AND b = ?'),
    ("SELECT * FROM t WHERE path = 'C:\\\\dir\\\\' AND n = 5", None, 'SELECT * FROM t WHERE path = ? AND n = ?'),
    ('INSERT INTO t (a, b) VALUES (-5, +3.25e+2)', None, 'INSERT INTO t (a, b) VALUES (-?, +?)'),
    ("SELECT * FROM t WHERE a = 'secret\\", None, 'SELECT * FROM t WHERE a = ?'),
    ('SELECT * FROM t WHERE a = "secret\\', 'mysql', 'SELECT * FROM t WHERE a = ?'),
]


@pytest.mark.parametrize(("statement", "system", "expected"), SERVER_CORPUS)
def test_mask_matches_the_server_for_every_corpus_case(statement, system, expected):
    assert sql_statement.mask(statement, system) == expected
    assert sql_statement.mask(expected, system) == expected  # idempotent


def test_mask_for_span_passes_the_system_through():
    assert sql_statement.mask_for_span('SELECT * FROM t WHERE token = "abc"', "mysql") == "SELECT * FROM t WHERE token = ?"
    assert sql_statement.mask_for_span('SELECT * FROM t WHERE token = "abc"') == 'SELECT * FROM t WHERE token = "abc"'


def test_objects_finds_a_stored_procedure_with_schema():
    assert sql_statement.objects("EXEC dbo.sp_refund_order @id = ?") == {
        "operation": "EXEC",
        "procedures": ["dbo.sp_refund_order"],
        "relations": [],
    }


def test_objects_finds_views_tables_and_table_functions():
    found = sql_statement.objects("SELECT * FROM v_totals t JOIN public.customers c ON c.id = t.id")
    assert found["relations"] == ["v_totals", "public.customers"]
    assert sql_statement.objects("SELECT * FROM get_open_orders(?) o")["procedures"] == ["get_open_orders"]


def test_objects_does_not_misread_column_lists_or_builtins():
    assert sql_statement.objects("INSERT INTO audit_log (a) VALUES (?)")["procedures"] == []
    assert sql_statement.objects("SELECT count(*) FROM orders")["procedures"] == []
    assert sql_statement.objects("garbage") is None


def _config(**overrides):
    config = Configuration()
    config.environment = "production"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


ERROR = FakeDbError("boom", "EXEC dbo.sp_refund_order @order_id = 8814, @note = 'a@b.co'")


def test_builder_sends_the_procedure_name_but_not_the_statement_by_default():
    payload = EventBuilder(_config()).build(ERROR)
    assert payload["sql_objects"]["procedures"] == ["dbo.sp_refund_order"]
    assert "sql_statement" not in payload


def test_builder_sends_the_masked_statement_when_opted_in():
    payload = EventBuilder(_config(capture_sql_statement=True)).build(ERROR)
    assert payload["sql_statement"] == "EXEC dbo.sp_refund_order @order_id = ?, @note = ?"


def test_builder_sends_neither_when_both_are_off():
    payload = EventBuilder(_config(capture_sql_objects=False)).build(ERROR)
    assert "sql_objects" not in payload and "sql_statement" not in payload


def test_builder_adds_nothing_for_an_error_with_no_sql():
    payload = EventBuilder(_config()).build(ValueError("nope"))
    assert "sql_objects" not in payload and "sql_statement" not in payload
