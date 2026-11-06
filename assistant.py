"""The NL -> SQL pipeline.

    question
       |  build prompt (live schema + date context + business definitions)
       v
     LLM  -->  parse reply  -->  validate (static)  -->  execute (read-only DB)
       ^                              |                        |
       |      "invalid" / DB error: feed the error back, retry (bounded)
       |      "unsafe": stop immediately, never retry, log it
       +------------------------------+------------------------+

Every request is optionally appended to a JSONL audit log (question, SQL, verdict,
errors; never result rows).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .executor import Database, QueryExecutionError, QueryTimeout
from .llm import LLMClient, LLMError
from .prompts import build_system_prompt, parse_reply
from .validator import SQLValidationError, validate_sql


@dataclass
class Attempt:
    stage: str  # generate | validate | execute | ok
    sql: str | None = None
    error: str | None = None


def format_table(columns: list[str], rows: list[tuple], max_cell: int = 40) -> str:
    if not columns:
        return ""

    def fmt(v) -> str:
        s = "NULL" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))
        return s if len(s) <= max_cell else s[: max_cell - 1] + "…"

    body = [[fmt(v) for v in row] for row in rows]
    widths = [max([len(c)] + [len(r[i]) for r in body]) for i, c in enumerate(columns)]
    numeric = [
        bool(rows) and all(r[i] is None or (isinstance(r[i], (int, float)) and not isinstance(r[i], bool)) for r in rows)
        for i in range(len(columns))
    ]

    def pad(text: str, i: int) -> str:
        return text.rjust(widths[i]) if numeric[i] else text.ljust(widths[i])

    out = ["  ".join(pad(c, i) for i, c in enumerate(columns)), "  ".join("-" * w for w in widths)]
    out += ["  ".join(pad(v, i) for i, v in enumerate(r)) for r in body]
    return "\n".join(line.rstrip() for line in out)


@dataclass
class QueryResult:
    question: str
    status: str  # ok | rejected | unanswerable | error
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    message: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_table(self, max_cell: int = 40) -> str:
        return format_table(self.columns, self.rows, max_cell)


class NL2SQLAssistant:
    def __init__(
        self,
        db: Database,
        llm: LLMClient,
        *,
        glossary: list[str] | None = None,
        max_attempts: int = 2,
        max_rows: int = 200,
        timeout_s: float = 5.0,
        today: date | Callable[[], date] | None = None,
        audit_log: str | Path | None = None,
    ):
        self.db = db
        self.llm = llm
        self.glossary = glossary or []
        self.max_attempts = max(1, max_attempts)
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self._today = today
        self.audit_log = Path(audit_log) if audit_log else None

    def _resolve_today(self) -> date:
        if callable(self._today):
            return self._today()
        return self._today or date.today()

    def ask(self, question: str, *, execute: bool = True) -> QueryResult:
        """Answer ``question``. With ``execute=False`` only generate and validate the SQL."""
        started = time.perf_counter()
        result = QueryResult(question=(question or "").strip(), status="error")
        if not result.question:
            result.message = "Empty question"
            return self._finish(result, started)

        schema = self.db.schema
        system = build_system_prompt(schema, self._resolve_today(), self.glossary, self.db.dialect)
        messages = [{"role": "user", "content": f"Question: {result.question}"}]
        last_error = "unknown error"

        for _ in range(self.max_attempts):
            try:
                reply = self.llm.complete(system, messages)
            except LLMError as exc:
                result.message = f"LLM request failed: {exc}"
                return self._finish(result, started)

            sql, refusal = parse_reply(reply)
            if sql is None and refusal is not None:
                result.status = "unanswerable"
                result.message = refusal
                return self._finish(result, started)

            if sql is None:
                last_error = "The reply did not contain a SQL query. Reply with one query in a ```sql block."
                result.attempts.append(Attempt("generate", None, last_error))
            else:
                try:
                    validated = validate_sql(
                        sql, schema.table_names, allowed_schemas=self.db.allowed_schemas
                    )
                except SQLValidationError as exc:
                    result.attempts.append(Attempt("validate", sql, str(exc)))
                    result.sql = sql
                    if exc.kind == "unsafe":
                        result.status = "rejected"
                        result.message = str(exc)
                        return self._finish(result, started)
                    last_error = str(exc)
                else:
                    result.sql = validated.sql
                    if not execute:
                        result.status = "ok"
                        result.attempts.append(Attempt("ok", validated.sql))
                        return self._finish(result, started)
                    try:
                        out = self.db.execute_readonly(
                            validated.sql, max_rows=self.max_rows, timeout_s=self.timeout_s
                        )
                    except QueryTimeout as exc:
                        last_error = f"{exc}. Write a cheaper query (filter earlier, avoid cross joins)."
                        result.attempts.append(Attempt("execute", validated.sql, str(exc)))
                    except QueryExecutionError as exc:
                        last_error = f"The database returned an error: {exc}"
                        result.attempts.append(Attempt("execute", validated.sql, str(exc)))
                    else:
                        result.status = "ok"
                        result.columns, result.rows, result.truncated = out.columns, out.rows, out.truncated
                        result.attempts.append(Attempt("ok", validated.sql))
                        return self._finish(result, started)

            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": f"That did not work: {last_error}\nReturn a corrected query in a ```sql block.",
                }
            )

        result.status = "error"
        result.message = f"No working query after {self.max_attempts} attempt(s). Last error: {last_error}"
        return self._finish(result, started)

    def _finish(self, result: QueryResult, started: float) -> QueryResult:
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if self.audit_log:
            record = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "question": result.question,
                "status": result.status,
                "sql": result.sql,
                "message": result.message,
                "attempts": [{"stage": a.stage, "error": a.error} for a in result.attempts],
                "row_count": len(result.rows),
                "elapsed_ms": result.elapsed_ms,
            }
            with self.audit_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        return result
