"""Read-only query execution (layers 2-4 of the safety stack).

Even if a query slips past the validator, the database itself refuses to do harm:

  2. the file is opened read-only (``mode=ro``)          -> writes fail at the OS/VFS level
  3. ``PRAGMA query_only = ON``                          -> writes fail at the SQL level
  4. a SQLite *authorizer* callback allows only SELECT,  -> every other action (DDL, ATTACH,
     reads of allow-listed tables, and benign functions     PRAGMA, reads of hidden tables,
                                                           load_extension...) is denied
plus resource limits: a wall-clock timeout (progress handler), a hard row cap, and a cap on
the size of any single string/blob (so ``SELECT randomblob(1000000000)`` cannot eat the RAM).

The PostgreSQL equivalent of these layers is a dedicated read-only role plus
``SET TRANSACTION READ ONLY`` and ``statement_timeout``; see ``pg_backend.py`` and
``docs/postgres_readonly_role.sql``.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .schema import Schema, introspect_sqlite

# Numeric fallbacks keep this working on Python builds that don't export the constants.
_SQLITE_OK = getattr(sqlite3, "SQLITE_OK", 0)
_SQLITE_DENY = getattr(sqlite3, "SQLITE_DENY", 1)
_SQLITE_SELECT = getattr(sqlite3, "SQLITE_SELECT", 21)
_SQLITE_READ = getattr(sqlite3, "SQLITE_READ", 20)
_SQLITE_FUNCTION = getattr(sqlite3, "SQLITE_FUNCTION", 31)
_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)

_BLOCKED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})


class QueryExecutionError(Exception):
    """The database rejected or failed to run the query."""


class QueryTimeout(QueryExecutionError):
    """The query exceeded its time budget."""


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_ms: float


class Database(Protocol):
    """What the assistant needs from a database backend."""

    dialect: str
    allowed_schemas: tuple[str, ...]

    @property
    def schema(self) -> Schema: ...

    def execute_readonly(self, sql: str, *, max_rows: int = 200, timeout_s: float = 5.0) -> ExecResult: ...


def _make_authorizer(allowed_tables: list[str], stored_tables: set[str]):
    allowed = {t.lower() for t in allowed_tables}
    stored = {t.lower() for t in stored_tables}

    def authorizer(action, arg1, arg2, dbname, _source):
        if action in (_SQLITE_SELECT, _SQLITE_RECURSIVE):
            return _SQLITE_OK
        if action == _SQLITE_READ:
            # arg1 = table name, arg2 = column ('' for e.g. COUNT(*)), dbname = 'main' | 'temp' | None.
            if dbname not in (None, "main"):
                return _SQLITE_DENY
            name = (arg1 or "").lower()
            if name in allowed:
                return _SQLITE_OK
            if name in stored or name.startswith("sqlite_"):
                return _SQLITE_DENY  # a real table the assistant may not see, or sqlite_master
            # Anything else is a CTE / subquery alias. SQLite reports reads of its underlying
            # stored tables as separate READ events, which are checked by the rules above.
            return _SQLITE_OK
        if action == _SQLITE_FUNCTION:
            if arg2 and arg2.lower() in _BLOCKED_FUNCTIONS:
                return _SQLITE_DENY
            return _SQLITE_OK
        return _SQLITE_DENY  # INSERT/UPDATE/DELETE/CREATE/DROP/PRAGMA/ATTACH/... all land here

    return authorizer


class SQLiteDatabase:
    dialect = "SQLite"
    allowed_schemas = ("main",)

    def __init__(
        self,
        path: str | Path,
        *,
        include_tables: list[str] | None = None,
        exclude_tables: list[str] | tuple[str, ...] = (),
        value_hints: bool = True,
        max_value_bytes: int = 1_000_000,
    ):
        self.path = Path(path)
        self.max_value_bytes = max_value_bytes
        self._include = include_tables
        self._exclude = tuple(exclude_tables)
        self._value_hints = value_hints
        self._schema: Schema | None = None

    @property
    def schema(self) -> Schema:
        if self._schema is None:
            self._schema = introspect_sqlite(
                self.path, include=self._include, exclude=self._exclude, value_hints=self._value_hints
            )
        return self._schema

    def refresh_schema(self) -> Schema:
        self._schema = None
        return self.schema

    def _connect(self) -> sqlite3.Connection:
        uri = self.path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.execute("PRAGMA query_only = ON")
        if hasattr(conn, "setlimit"):  # Python 3.11+: cap any single string/blob (stops randomblob(1e9) etc.)
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.max_value_bytes)
        stored = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}  # before the authorizer is on
        conn.set_authorizer(_make_authorizer(self.schema.table_names, stored))
        return conn

    def execute_readonly(self, sql: str, *, max_rows: int = 200, timeout_s: float = 5.0) -> ExecResult:
        started = time.perf_counter()
        conn = self._connect()
        try:
            deadline = time.monotonic() + timeout_s
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            try:
                cur = conn.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1)
            except sqlite3.Error as exc:
                if "interrupted" in str(exc).lower():
                    raise QueryTimeout(f"Query exceeded the {timeout_s:g}s time limit") from exc
                raise QueryExecutionError(str(exc)) from exc
        finally:
            conn.close()
        truncated = len(rows) > max_rows
        return ExecResult(columns, rows[:max_rows], truncated, round((time.perf_counter() - started) * 1000, 1))
