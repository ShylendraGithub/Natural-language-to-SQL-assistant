import pytest

from nl2sql.validator import SQLValidationError, validate_sql

TABLES = ["customers", "orders", "order_items", "products"]

SAFE = [
    "SELECT * FROM customers",
    "select name from customers where city = 'DROP TABLE x'",  # keyword inside a literal
    "SELECT name FROM customers WHERE name LIKE 'A%'",
    "SELECT replace(name, 'a', 'b') FROM customers",  # replace() is a function, not a statement
    "SELECT c.name, SUM(oi.quantity * oi.unit_price) AS r FROM customers c "
    "JOIN orders o ON o.customer_id = c.customer_id "
    "JOIN order_items oi ON oi.order_id = o.order_id GROUP BY c.name ORDER BY r DESC LIMIT 5;",
    "WITH t AS (SELECT customer_id FROM orders) SELECT * FROM t",
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT x FROM n",
    "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT * FROM b",
    "SELECT * FROM (SELECT customer_id FROM orders) AS s",
    "SELECT a.name FROM customers a, orders b WHERE a.customer_id = b.customer_id",
    'SELECT "name" FROM "customers"',
    "SELECT * FROM main.customers",
    "SELECT 1 -- trailing comment",
    "/* leading */ SELECT 1",
    "SELECT * FROM Customers",  # case-insensitive
]

UNSAFE = [
    "DROP TABLE customers",
    "DELETE FROM orders",
    "UPDATE customers SET name = 'x'",
    "INSERT INTO customers VALUES (1)",
    "CREATE TABLE x (a)",
    "VACUUM",
    "PRAGMA table_info(customers)",
    "ATTACH DATABASE 'x.db' AS x",
    "SELECT 1; DROP TABLE customers",
    "SELECT 1; SELECT 2",
    "SELECT 1 /* x */; /* y */ DELETE FROM orders",
    "WITH x AS (SELECT 1) DELETE FROM orders",
    "SELECT * INTO newtable FROM customers",
    "SELECT load_extension('evil')",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM main.sqlite_master",
    "SELECT * FROM temp.sqlite_temp_master",
    "SELECT * FROM information_schema.tables",
    "EXPLAIN SELECT 1",
]


@pytest.mark.parametrize("sql", SAFE)
def test_safe_queries_pass(sql):
    validate_sql(sql, TABLES)


@pytest.mark.parametrize("sql", UNSAFE)
def test_unsafe_queries_are_rejected_as_unsafe(sql):
    with pytest.raises(SQLValidationError) as exc:
        validate_sql(sql, TABLES)
    assert exc.value.kind == "unsafe"


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "sqlite_master"',
        "SELECT * FROM [sqlite_master]",
        "SELECT * FROM `sqlite_master`",
        "SELECT 1 UNION SELECT sql FROM sqlite_master",
        "SELECT name FROM customers UNION ALL SELECT sql FROM sqlite_schema",
        "SELECT 1 WHERE 1=1 -- '\n; DROP TABLE customers",  # semicolon after a line comment
        "SELECT 1 ; DROP TABLE customers",  # Unicode line separator is not a statement boundary to SQLite
    ],
)
def test_catalog_access_and_smuggling_tricks_are_rejected(sql):
    with pytest.raises(SQLValidationError):
        validate_sql(sql, TABLES)


def test_virtual_table_functions_are_not_tables():
    for sql in ("SELECT * FROM json_each('[1]')", "SELECT * FROM pragma_table_info('customers')"):
        with pytest.raises(SQLValidationError):
            validate_sql(sql, TABLES)


def test_non_standard_whitespace_is_refused_rather_than_guessed_at():
    with pytest.raises(SQLValidationError, match="U\\+00A0"):
        validate_sql("SELECT * FROM customers", TABLES)
    validate_sql("SELECT 'a b' FROM customers", TABLES)  # fine inside a string literal


@pytest.mark.parametrize("sql", ["", "   ", "-- only a comment", "SELECT 'unterminated", "SELECT 1 /* open"])
def test_malformed_queries_are_invalid_not_unsafe(sql):
    with pytest.raises(SQLValidationError) as exc:
        validate_sql(sql, TABLES)
    assert exc.value.kind == "invalid"


def test_unknown_table_is_invalid_and_lists_alternatives():
    with pytest.raises(SQLValidationError) as exc:
        validate_sql("SELECT * FROM users", TABLES)
    assert exc.value.kind == "invalid"
    assert "users" in str(exc.value) and "customers" in str(exc.value)


def test_unknown_table_inside_join_and_subquery():
    for sql in (
        "SELECT * FROM customers c JOIN secrets s ON s.id = c.customer_id",
        "SELECT * FROM customers WHERE customer_id IN (SELECT id FROM secrets)",
        "SELECT * FROM customers, secrets",
    ):
        with pytest.raises(SQLValidationError):
            validate_sql(sql, TABLES)


def test_hidden_table_is_rejected_even_though_it_exists():
    with pytest.raises(SQLValidationError):
        validate_sql("SELECT * FROM orders", ["customers"])


def test_sanitised_sql_drops_comments_and_trailing_semicolon():
    out = validate_sql("SELECT 1; -- bye", TABLES)
    assert out.sql == "SELECT 1"
    out = validate_sql("/* hi */ SELECT name FROM customers -- c\n;", TABLES)
    assert ";" not in out.sql and "--" not in out.sql and "/*" not in out.sql


def test_tables_reported_without_ctes():
    out = validate_sql(
        "WITH t AS (SELECT * FROM orders) SELECT * FROM t JOIN Customers c ON c.customer_id = t.customer_id", TABLES
    )
    assert out.tables == ("customers", "orders")


def test_keywords_in_identifiers_and_strings_are_not_flagged():
    validate_sql("SELECT name AS \"delete\" FROM customers", TABLES)  # quoted identifier
    validate_sql("SELECT 'a;b' AS x FROM customers", TABLES)  # semicolon inside a literal
    validate_sql("SELECT signup_date AS updated_at FROM customers", TABLES)  # longer word


def test_extract_from_is_not_a_table_reference():
    validate_sql("SELECT EXTRACT(YEAR FROM signup_date) FROM customers", TABLES)
