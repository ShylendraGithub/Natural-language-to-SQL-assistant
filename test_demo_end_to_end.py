"""End-to-end checks with the offline demo model against the seeded database.

The headline test recomputes "top 5 customers by revenue last quarter" in plain Python
(no SQL aggregation) and compares it with what the pipeline returned.
"""

import sqlite3
from collections import defaultdict
from datetime import date

from nl2sql.assistant import NL2SQLAssistant
from nl2sql.demo_llm import DemoLLM
from nl2sql.evaluate import evaluate, resolve_tokens
from nl2sql.seed import DEMO_GLOSSARY

import json
from pathlib import Path

from conftest import TODAY, ScriptedLLM, fence


def assistant(db, **kw):
    return NL2SQLAssistant(db, DemoLLM(), glossary=DEMO_GLOSSARY, today=TODAY, **kw)


def python_top_customers(db_path, start, end, n=5):
    conn = sqlite3.connect(db_path)
    try:
        names = dict(conn.execute("SELECT customer_id, name FROM customers"))
        revenue = defaultdict(float)
        rows = conn.execute(
            "SELECT o.customer_id, o.status, o.order_date, oi.quantity, oi.unit_price "
            "FROM orders o JOIN order_items oi ON oi.order_id = o.order_id"
        )
        for cid, status, odate, qty, price in rows:
            if status == "completed" and start <= odate < end:
                revenue[cid] += qty * price
    finally:
        conn.close()
    ranked = sorted(revenue.items(), key=lambda kv: (-round(kv[1], 2), kv[0]))[:n]
    return [(cid, names[cid], round(total, 2)) for cid, total in ranked]


def test_top_5_customers_by_revenue_last_quarter_matches_independent_calculation(db, db_path):
    r = assistant(db).ask("top 5 customers by revenue last quarter")
    assert r.ok, r.message
    assert r.columns == ["customer_id", "name", "revenue"]
    assert "2026-07-01" in r.sql and "2026-10-01" in r.sql  # the date range came from the prompt
    assert [tuple(row) for row in r.rows] == python_top_customers(db_path, "2026-07-01", "2026-10-01")


def test_revenue_excludes_cancelled_and_refunded_orders(db):
    r = assistant(db).ask("top 5 customers by revenue last quarter")
    assert "status = 'completed'" in r.sql


def test_destructive_request_is_blocked_and_data_survives(db, db_path):
    conn = sqlite3.connect(db_path)
    before = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled'").fetchone()[0]
    conn.close()
    assert before > 0
    r = assistant(db).ask("delete all cancelled orders")
    assert r.status == "rejected"
    conn = sqlite3.connect(db_path)
    after = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled'").fetchone()[0]
    conn.close()
    assert after == before


def test_self_correction_loop_recovers_from_a_hallucinated_column(db):
    r = assistant(db).ask("how many customers per country?")
    assert r.ok
    assert [a.stage for a in r.attempts] == ["execute", "ok"]
    assert sum(row[1] for row in r.rows) == 60


def test_unsupported_question_is_declined(db):
    r = assistant(db).ask("what's the weather in Paris?")
    assert r.status == "unanswerable"


def test_other_period_phrases(db):
    for phrase, expected in (("last month", ("2026-10-01", "2026-11-01")), ("this year", ("2026-01-01", "2027-01-01"))):
        r = assistant(db).ask(f"revenue by category {phrase}")
        assert r.ok, r.message
        assert expected[0] in r.sql and expected[1] in r.sql


def test_evaluation_harness_scores_correct_and_wrong_answers(db):
    questions = [
        {"question": "q1", "gold_sql": "SELECT COUNT(*) FROM customers"},
        {"question": "q2", "gold_sql": "SELECT COUNT(*) FROM customers"},
        {"question": "q3", "expect": "refuse"},
        {"question": "q4", "expect": "refuse"},
    ]
    llm = ScriptedLLM(
        fence("SELECT COUNT(*) AS n FROM customers"),  # right
        fence("SELECT COUNT(*) FROM orders"),          # wrong result
        fence("DELETE FROM orders"),                   # blocked -> counts as a good refusal
        fence("SELECT COUNT(*) FROM orders"),          # complied with a "refuse" case -> fail
    )
    report = evaluate(NL2SQLAssistant(db, llm, today=TODAY), db, questions, TODAY)
    assert (report["correct"], report["answerable"]) == (1, 2)
    assert (report["refusals_correct"], report["refusal_cases"]) == (1, 2)
    assert report["execution_accuracy"] == 0.5


def test_gold_queries_in_the_bundled_eval_set_all_run(db):
    questions = json.loads((Path(__file__).resolve().parent.parent / "eval" / "questions.json").read_text())
    for item in questions:
        if "gold_sql" in item:
            out = db.execute_readonly(resolve_tokens(item["gold_sql"], TODAY), max_rows=100_000)
            assert out.rows, item["question"]
    assert resolve_tokens("a LQ_START b LQ_END", date(2026, 11, 6)) == "a 2026-07-01 b 2026-10-01"
