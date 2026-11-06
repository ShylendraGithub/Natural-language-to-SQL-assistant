import json
import sqlite3

from nl2sql.assistant import NL2SQLAssistant, format_table
from nl2sql.llm import LLMError

from conftest import TODAY, ScriptedLLM, fence


def make(db, llm, **kw):
    kw.setdefault("today", TODAY)
    return NL2SQLAssistant(db, llm, **kw)


def order_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        conn.close()


def test_happy_path(db):
    llm = ScriptedLLM(fence("SELECT COUNT(*) AS n FROM customers;"))
    r = make(db, llm).ask("how many customers?")
    assert r.status == "ok" and r.ok
    assert r.rows == [(60,)] and r.columns == ["n"]
    assert r.sql == "SELECT COUNT(*) AS n FROM customers"  # semicolon stripped
    assert len(llm.calls) == 1


def test_prompt_sent_to_llm_has_schema_dates_and_question(db):
    llm = ScriptedLLM(fence("SELECT 1"))
    make(db, llm, glossary=["revenue = completed orders only"]).ask("top customers last quarter")
    system, messages = llm.calls[0]
    assert "TABLE customers (" in system
    assert "Last quarter: 2026-07-01 <= date < 2026-10-01" in system
    assert "revenue = completed orders only" in system
    assert messages == [{"role": "user", "content": "Question: top customers last quarter"}]


def test_database_error_is_fed_back_and_retried(db):
    llm = ScriptedLLM(fence("SELECT nation FROM customers"), fence("SELECT COUNT(DISTINCT country) FROM customers"))
    r = make(db, llm).ask("how many countries?")
    assert r.ok and len(llm.calls) == 2
    feedback = llm.calls[1][1][-1]["content"]
    assert "no such column" in feedback
    assert [a.stage for a in r.attempts] == ["execute", "ok"]


def test_unknown_table_is_retried_with_available_tables(db):
    llm = ScriptedLLM(fence("SELECT * FROM users"), fence("SELECT COUNT(*) FROM customers"))
    r = make(db, llm).ask("how many users?")
    assert r.ok
    assert "Available tables" in llm.calls[1][1][-1]["content"]


def test_unsafe_sql_is_blocked_and_never_retried(db, db_path):
    before = order_count(db_path)
    llm = ScriptedLLM(fence("DELETE FROM orders"), fence("SELECT 1"))
    r = make(db, llm).ask("remove all orders")
    assert r.status == "rejected"
    assert "Only SELECT" in r.message
    assert len(llm.calls) == 1  # no second chance
    assert order_count(db_path) == before


def test_multi_statement_injection_is_blocked(db, db_path):
    before = order_count(db_path)
    llm = ScriptedLLM(fence("SELECT 1; DROP TABLE orders"))
    r = make(db, llm).ask("anything")
    assert r.status == "rejected" and "Multiple statements" in r.message
    assert order_count(db_path) == before


def test_model_can_decline(db):
    llm = ScriptedLLM("CANNOT_ANSWER: there is no salary data in this schema")
    r = make(db, llm).ask("what is the CEO's salary?")
    assert r.status == "unanswerable"
    assert "salary" in r.message
    assert r.sql is None


def test_gives_up_after_max_attempts(db):
    llm = ScriptedLLM(fence("SELECT nope FROM customers"), fence("SELECT nada FROM customers"), fence("SELECT 1"))
    r = make(db, llm, max_attempts=2).ask("q")
    assert r.status == "error"
    assert len(llm.calls) == 2
    assert "No working query after 2 attempt(s)" in r.message


def test_reply_without_sql_is_retried(db):
    llm = ScriptedLLM("You should look at the customers table.", fence("SELECT COUNT(*) FROM customers"))
    r = make(db, llm).ask("how many customers?")
    assert r.ok and len(llm.calls) == 2


def test_llm_failure_is_reported_not_raised(db):
    class Broken:
        def complete(self, system, messages):
            raise LLMError("rate limited")

    r = make(db, Broken()).ask("anything")
    assert r.status == "error" and "rate limited" in r.message


def test_empty_question(db):
    llm = ScriptedLLM()
    assert make(db, llm).ask("   ").status == "error"
    assert llm.calls == []


def test_dry_run_validates_but_does_not_execute(db):
    llm = ScriptedLLM(fence("SELECT * FROM orders"))
    r = make(db, llm).ask("show orders", execute=False)
    assert r.ok and r.rows == [] and r.sql == "SELECT * FROM orders"


def test_row_cap_is_reported(db):
    llm = ScriptedLLM(fence("SELECT * FROM orders"))
    r = make(db, llm, max_rows=25).ask("all orders")
    assert r.ok and len(r.rows) == 25 and r.truncated


def test_audit_log_records_decisions_but_not_data(db, tmp_path):
    log = tmp_path / "audit.jsonl"
    llm = ScriptedLLM(fence("SELECT name, email FROM customers LIMIT 2"), fence("DROP TABLE customers"))
    a = make(db, llm, audit_log=log)
    a.ask("show two customers")
    a.ask("drop the customers table")
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["status"] for r in records] == ["ok", "rejected"]
    assert records[0]["row_count"] == 2
    assert "@example.com" not in log.read_text()  # result rows never reach the log


def test_format_table_alignment_and_formatting():
    text = format_table(["name", "revenue"], [("Asha", 1234.5), ("B", None)])
    lines = text.splitlines()
    assert lines[0].startswith("name") and "revenue" in lines[0]
    assert "1,234.50" in lines[2] and "NULL" in lines[3]
    assert format_table([], []) == ""
