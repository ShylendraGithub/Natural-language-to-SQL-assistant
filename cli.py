"""Command-line interface.

    python -m nl2sql --demo "top 5 customers by revenue last quarter"
    python -m nl2sql --db sales.db "top 5 customers by revenue last quarter"
    python -m nl2sql --db sales.db --dry-run "..."     # show the SQL, don't run it
    python -m nl2sql --db sales.db --show-schema
    python -m nl2sql --demo                            # interactive prompt
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from .assistant import NL2SQLAssistant, QueryResult
from .demo_llm import DemoLLM
from .executor import SQLiteDatabase
from .llm import AnthropicClient, LLMError
from .seed import DEMO_GLOSSARY, seed_database

DEMO_DB = "demo_sales.db"


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines); existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nl2sql", description="Ask your database questions in plain English.")
    p.add_argument("question", nargs="*", help="the question (omit for an interactive prompt)")
    p.add_argument("--db", help="SQLite file, or a postgresql:// URL (needs the optional Postgres extras)")
    p.add_argument("--demo", action="store_true", help=f"use the sample database ({DEMO_DB}) and the offline demo model")
    p.add_argument("--dry-run", action="store_true", help="generate and validate the SQL but do not run it")
    p.add_argument("--show-schema", action="store_true", help="print the schema exactly as the model sees it")
    p.add_argument("--model", help="override the model id (default: $NL2SQL_MODEL or the built-in default)")
    p.add_argument("--max-rows", type=int, default=200)
    p.add_argument("--timeout", type=float, default=5.0, help="seconds before a query is cancelled")
    p.add_argument("--exclude-tables", default="", help="comma-separated tables the assistant must not see")
    p.add_argument("--no-value-hints", action="store_true", help="do not send example column values to the LLM")
    p.add_argument("--glossary", help="text file with one business definition per line")
    p.add_argument("--audit-log", help="append every request to this JSONL file")
    return p


def _open_db(args):
    exclude = [t.strip() for t in args.exclude_tables.split(",") if t.strip()]
    hints = not args.no_value_hints
    if args.demo:
        path = Path(args.db or DEMO_DB)
        if not path.exists():
            seed_database(path)
            print(f"[demo] created sample database {path}", file=sys.stderr)
        return SQLiteDatabase(path, exclude_tables=exclude, value_hints=hints)
    if not args.db:
        raise SystemExit("error: pass --db PATH (or --demo to try the sample database)")
    if args.db.startswith(("postgresql://", "postgresql+", "postgres://")):
        from .pg_backend import PostgresDatabase

        return PostgresDatabase(args.db, exclude_tables=exclude)
    if not Path(args.db).is_file():
        raise SystemExit(f"error: database file not found: {args.db}")
    return SQLiteDatabase(args.db, exclude_tables=exclude, value_hints=hints)


def print_result(result: QueryResult) -> None:
    if result.sql:
        print("SQL:")
        for line in result.sql.splitlines():
            print(f"  {line}")
        print()
    if result.status == "ok":
        if result.columns:
            print(result.to_table())
            note = f"{len(result.rows)} row(s)" + (" (truncated)" if result.truncated else "")
            retries = len(result.attempts) - 1
            if retries:
                note += f", {retries} retry"
            print(f"\n{note} in {result.elapsed_ms:g} ms")
        else:
            print("(dry run: query validated, not executed)")
    elif result.status == "rejected":
        print(f"BLOCKED: {result.message}")
    elif result.status == "unanswerable":
        print(f"Cannot answer: {result.message}")
    else:
        print(f"Error: {result.message}")


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        db = _open_db(args)
        if args.show_schema:
            print(db.schema.to_prompt())
            return 0
        llm = DemoLLM() if args.demo else AnthropicClient(model=args.model)
        glossary = list(DEMO_GLOSSARY) if args.demo else []
        if args.glossary:
            glossary += [
                ln.strip() for ln in Path(args.glossary).read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        assistant = NL2SQLAssistant(
            db, llm, glossary=glossary, max_rows=args.max_rows, timeout_s=args.timeout, audit_log=args.audit_log
        )
    except (LLMError, sqlite3.Error, OSError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.demo:
        print("[demo] offline rule-based model in use, no LLM API call is made", file=sys.stderr)

    question = " ".join(args.question).strip()
    if question:
        result = assistant.ask(question, execute=not args.dry_run)
        print_result(result)
        return 0 if result.status in ("ok", "unanswerable") else 1

    print("Ask a question about your data (type 'exit' to quit).")
    while True:
        try:
            question = input("\nnl2sql> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in ("exit", "quit", "q"):
            return 0
        if question:
            print_result(assistant.ask(question, execute=not args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
