from forge_ops_tracker import _query_transaction_name


def test_extracts_the_table_from_a_select_with_a_where_clause():
    sql = 'SELECT "auth_user"."id", "auth_user"."username" FROM "auth_user" WHERE "auth_user"."id" = %s'

    assert _query_transaction_name(sql) == "SELECT auth_user"


def test_extracts_the_table_from_a_delete():
    sql = 'DELETE FROM "sessions" WHERE "sessions"."id" = %s'

    assert _query_transaction_name(sql) == "DELETE sessions"


def test_extracts_the_table_from_an_insert():
    sql = 'INSERT INTO "orders" ("id", "total") VALUES (%s, %s)'

    assert _query_transaction_name(sql) == "INSERT INTO orders"


def test_extracts_the_table_from_an_update():
    sql = 'UPDATE "orders" SET "total" = %s WHERE "orders"."id" = %s'

    assert _query_transaction_name(sql) == "UPDATE orders"


def test_extracts_the_table_regardless_of_quoting_style():
    # SQLAlchemy's own default dialect uses no quoting at all for a simple identifier; Django's
    # backends typically quote with double quotes (SQLite/Postgres) or backticks (MySQL). All
    # three need to resolve to the same low-cardinality name.
    assert _query_transaction_name("SELECT * FROM users WHERE id = ?") == "SELECT users"
    assert _query_transaction_name("SELECT * FROM `users` WHERE id = ?") == "SELECT users"


def test_falls_back_to_the_first_keyword_when_no_table_name_is_found():
    assert _query_transaction_name("SELECT 1") == "SELECT"
    assert _query_transaction_name("PRAGMA foreign_keys = ON") == "PRAGMA"
    assert _query_transaction_name("BEGIN") == "BEGIN"


def test_falls_back_to_a_literal_sql_for_a_blank_string():
    assert _query_transaction_name("") == "SQL"
    assert _query_transaction_name("   ") == "SQL"
