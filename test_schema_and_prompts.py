import datetime as dt
import sqlite3

import pytest

from nl2sql.prompts import (
    build_system_prompt,
    date_context,
    parse_reply,
    previous_month,
    previous_quarter,
)
from nl2sql.schema import introspect_sqlite

from conftest import TODAY


def test_introspection_finds_tables_columns_and_foreign_keys(db_path):
    schema = introspect_sqlite(db_path)
    assert schema.table_names == ["customers", "order_items", "orders", "products"]
    orders = next(t for t in schema.tables if t.name == "orders")
    assert {(fk.column, fk.ref_table, fk.ref_column) for fk in orders.foreign_keys} == {
        ("customer_id", "customers", "customer_id")
    }
    assert any(c.primary_key and c.name == "order_id" for c in orders.columns)


def test_low_cardinality_values_become_hints(db_path):
    schema = introspect_sqlite(db_path)
    status = next(c for t in schema.tables if t.name == "orders" for c in t.columns if c.name == "status")
    assert status.hints == ["'cancelled'", "'completed'", "'refunded'"]


def test_high_cardinality_and_personal_columns_get_no_hints(db_path):
    schema = introspect_sqlite(db_path)
    customers = next(t for t in schema.tables if t.name == "customers")
    for col in customers.columns:
        if col.name in ("name", "email", "signup_date"):
            assert col.hints == []


def test_value_hints_can_be_disabled(db_path):
    schema = introspect_sqlite(db_path, value_hints=False)
    assert all(not c.hints for t in schema.tables for c in t.columns)
    assert all(t.row_count is None for t in schema.tables)


def test_exclude_and_include(db_path):
    assert "orders" not in introspect_sqlite(db_path, exclude=["Orders"]).table_names
    assert introspect_sqlite(db_path, include=["products"]).table_names == ["products"]


def test_internal_tables_are_skipped(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY AUTOINCREMENT)")  # creates sqlite_sequence
    conn.commit()
    conn.close()
    assert introspect_sqlite(p).table_names == ["a"]


def test_system_prompt_contains_schema_dates_and_definitions_but_no_row_data(db):
    prompt = build_system_prompt(db.schema, TODAY, ["revenue = something"], db.dialect)
    assert "TABLE orders (" in prompt
    assert "FOREIGN KEY (customer_id) REFERENCES customers(customer_id)" in prompt
    assert "Last quarter: 2026-07-01 <= date < 2026-10-01" in prompt
    assert "- revenue = something" in prompt
    assert "@example.com" not in prompt  # no individual customer data


@pytest.mark.parametrize(
    "today, start, end",
    [
        (dt.date(2026, 11, 6), dt.date(2026, 7, 1), dt.date(2026, 10, 1)),
        (dt.date(2026, 1, 15), dt.date(2025, 10, 1), dt.date(2026, 1, 1)),  # crosses a year boundary
        (dt.date(2026, 3, 31), dt.date(2025, 10, 1), dt.date(2026, 1, 1)),
        (dt.date(2026, 4, 1), dt.date(2026, 1, 1), dt.date(2026, 4, 1)),
        (dt.date(2026, 12, 31), dt.date(2026, 7, 1), dt.date(2026, 10, 1)),
    ],
)
def test_previous_quarter(today, start, end):
    assert previous_quarter(today) == (start, end)


def test_previous_month_handles_january_and_leap_years():
    assert previous_month(dt.date(2026, 1, 10)) == (dt.date(2025, 12, 1), dt.date(2026, 1, 1))
    assert previous_month(dt.date(2024, 3, 5)) == (dt.date(2024, 2, 1), dt.date(2024, 3, 1))


def test_date_context_lists_all_ranges():
    text = date_context(TODAY)
    assert text.startswith("Today: 2026-11-06 (Friday)")
    for label in ("This month", "Last month", "This quarter", "Last quarter", "This year", "Last year"):
        assert f"{label}:" in text


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("```sql\nSELECT 1\n```", ("SELECT 1", None)),
        ("Here you go:\n```SQL\nSELECT 1;\n```\nHope it helps", ("SELECT 1;", None)),
        ("```\nSELECT 1\n```", ("SELECT 1", None)),
        ("SELECT 1", ("SELECT 1", None)),
        ("  with t as (select 1) select * from t", ("with t as (select 1) select * from t", None)),
        ("CANNOT_ANSWER: no salary data", (None, "no salary data")),
        ("```sql\nCANNOT_ANSWER: no salary data\n```", (None, "no salary data")),
        ("Sorry. CANNOT_ANSWER: no salary data", (None, "no salary data")),
        ("I think you should look at the orders table.", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_reply(reply, expected):
    assert parse_reply(reply) == expected
