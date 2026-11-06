"""Execution-accuracy evaluation.

For each question in a JSON file the harness
  * asks the assistant,
  * runs the hand-written gold SQL,
  * and counts the answer as correct when both return the same rows
    (order-insensitive, floats rounded to 2 dp, column names ignored).

Questions marked ``"expect": "refuse"`` pass only if the assistant declines or the
guardrails block the request; they measure the safety behaviour, not accuracy.

    python -m nl2sql.evaluate --db demo_sales.db            # needs ANTHROPIC_API_KEY
    python -m nl2sql.evaluate --db demo_sales.db --demo     # smoke-test the harness

Gold SQL may use the tokens LQ_START/LQ_END (last quarter), LM_START/LM_END (last month)
and TY_START/TY_END (this year); ends are exclusive.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .assistant import NL2SQLAssistant
from .demo_llm import DemoLLM
from .executor import SQLiteDatabase
from .llm import AnthropicClient, LLMError
from .prompts import date_ranges
from .seed import DEMO_GLOSSARY

DEFAULT_QUESTIONS = Path(__file__).resolve().parent.parent / "eval" / "questions.json"


def resolve_tokens(sql: str, today: date) -> str:
    ranges = date_ranges(today)
    mapping = {
        "LQ": ranges["Last quarter"],
        "LM": ranges["Last month"],
        "TY": ranges["This year"],
    }
    for key, (start, end) in mapping.items():
        sql = sql.replace(f"{key}_START", start.isoformat()).replace(f"{key}_END", end.isoformat())
    return sql


def normalize(rows) -> list[tuple]:
    def norm(v):
        return round(v, 2) if isinstance(v, float) else v

    return sorted((tuple(norm(v) for v in r) for r in rows), key=repr)


def evaluate(assistant: NL2SQLAssistant, db, questions: list[dict], today: date) -> dict:
    details = []
    answerable = correct = refusals = refusals_ok = 0
    for item in questions:
        result = assistant.ask(item["question"])
        if item.get("expect") == "refuse":
            refusals += 1
            passed = result.status in ("rejected", "unanswerable")
            refusals_ok += passed
        else:
            answerable += 1
            gold = db.execute_readonly(resolve_tokens(item["gold_sql"], today), max_rows=100_000)
            passed = result.ok and normalize(result.rows) == normalize(gold.rows)
            correct += passed
        details.append({"question": item["question"], "passed": bool(passed), "status": result.status,
                        "attempts": len(result.attempts), "sql": result.sql})
    return {
        "answerable": answerable,
        "correct": correct,
        "execution_accuracy": round(correct / answerable, 3) if answerable else None,
        "refusal_cases": refusals,
        "refusals_correct": refusals_ok,
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nl2sql.evaluate")
    ap.add_argument("--db", required=True)
    ap.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    ap.add_argument("--demo", action="store_true", help="use the offline rule-based model (smoke test only)")
    ap.add_argument("--model")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args(argv)

    try:
        llm = DemoLLM() if args.demo else AnthropicClient(model=args.model)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    db = SQLiteDatabase(args.db)
    today = date.today()
    report = evaluate(NL2SQLAssistant(db, llm, glossary=list(DEMO_GLOSSARY), today=today), db, questions, today)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    for d in report["details"]:
        print(f"[{'PASS' if d['passed'] else 'FAIL'}] {d['question']}  ({d['status']}, {d['attempts']} attempt(s))")
    print(
        f"\nExecution accuracy: {report['correct']}/{report['answerable']}"
        f" | refusals handled: {report['refusals_correct']}/{report['refusal_cases']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
