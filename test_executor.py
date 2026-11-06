import sqlite3
import time

import pytest

from nl2sql.executor import QueryExecutionError, QueryTimeout, SQLiteDatabase


def count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_select_returns_columns_and_rows(db):
    out = db.execute_readonly("SELECT COUNT(*) AS n FROM customers")
    assert out.columns == ["n"]
    assert out.rows == [(60,)]
    assert not out.truncated


def test_row_cap_sets_truncated_flag(db):
    out = db.execute_readonly("SELECT * FROM orders", max_rows=10)
    assert len(out.rows) == 10
    assert out.truncated


def test_no_truncation_when_result_fits(db):
    out = db.execute_readonly("SELECT * FROM customers LIMIT 3", max_rows=10)
    assert len(out.rows) == 3
    assert not out.truncated


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE customers SET name = 'x'",
        "DROP TABLE orders",
        "INSERT INTO customers (name, email, city, country, signup_date) VALUES ('a','b','c','d','e')",
        "CREATE TABLE evil (a)",
        "PRAGMA writable_schema = ON",
        "ATTACH DATABASE ':memory:' AS m",
        "SELECT * FROM sqlite_master",
        "SELECT load_extension('nope')",
    ],
)
def test_engine_blocks_dangerous_sql_even_without_the_validator(db, db_path, sql):
    """Layers 2-4 must hold on their own: these calls bypass validate_sql entirely."""
    before = {t: count(db_path, t) for t in ("customers", "orders", "order_items")}
    with pytest.raises(QueryExecutionError):
        db.execute_readonly(sql)
    assert {t: count(db_path, t) for t in before} == before


def test_runaway_query_is_cancelled(db):
    started = time.monotonic()
    with pytest.raises(QueryTimeout):
        db.execute_readonly(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c",
            timeout_s=0.3,
        )
    assert time.monotonic() - started < 5


def test_hidden_table_cannot_be_read(db_path):
    restricted = SQLiteDatabase(db_path, exclude_tables=["orders"])
    with pytest.raises(QueryExecutionError):
        restricted.execute_readonly("SELECT * FROM orders")
    assert restricted.execute_readonly("SELECT COUNT(*) FROM customers").rows == [(60,)]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM orders",  # COUNT(*) reads no column, must still be denied
        "SELECT * FROM (SELECT * FROM orders) AS s",
        "WITH x AS (SELECT * FROM orders) SELECT * FROM x",
        "SELECT COUNT(*) FROM sqlite_master",
        "SELECT COUNT(*) FROM customers WHERE customer_id IN (SELECT customer_id FROM orders)",
    ],
)
def test_hidden_table_is_unreachable_through_aggregates_subqueries_and_ctes(db_path, sql):
    restricted = SQLiteDatabase(db_path, exclude_tables=["orders"])
    with pytest.raises(QueryExecutionError):
        restricted.execute_readonly(sql)


def test_ctes_over_allowed_tables_still_work(db):
    out = db.execute_readonly(
        "WITH big AS (SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(*) > 5) "
        "SELECT COUNT(*) FROM big"
    )
    assert out.rows[0][0] > 0
    out = db.execute_readonly("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT SUM(x) FROM n")
    assert out.rows == [(15,)]


@pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="needs Python 3.11+")
def test_oversized_values_are_refused(db):
    assert len(db.execute_readonly("SELECT randomblob(1000)").rows[0][0]) == 1000
    for sql in ("SELECT randomblob(900000000)", "SELECT zeroblob(5000000)", "SELECT hex(zeroblob(2000000))"):
        with pytest.raises(QueryExecutionError, match="too big"):
            db.execute_readonly(sql)
    # printf degrades to NULL instead of erroring, but still never allocates past the cap
    assert db.execute_readonly("SELECT printf('%2000000d', 1)").rows == [(None,)]


def test_sql_errors_surface_as_query_execution_error(db):
    with pytest.raises(QueryExecutionError, match="no such column"):
        db.execute_readonly("SELECT nope FROM customers")
