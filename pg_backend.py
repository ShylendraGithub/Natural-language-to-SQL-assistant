"""PostgreSQL backend via SQLAlchemy (optional).

STATUS: reference implementation, written against the SQLAlchemy 2.x API but NOT
exercised by this project's test-suite (it needs SQLAlchemy and a Postgres server).
Treat it as a starting point: run it once against a scratch database before relying
on it.

Defence in depth for Postgres, from strongest to weakest:
  1. connect as a dedicated role that only has SELECT on the allowed tables
     (see docs/postgres_readonly_role.sql)  <- the real security boundary
  2. ``SET TRANSACTION READ ONLY``            <- refuses writes even if the role could
  3. ``SET LOCAL statement_timeout``          <- bounds runtime
  4. the static validator and row cap, same as for SQLite
"""

from __future__ import annotations

import re
import time

from .executor import ExecResult, QueryExecutionError, QueryTimeout
from .schema import Column, ForeignKey, Schema, Table

try:  # imported lazily so the rest of the package works without SQLAlchemy
    import sqlalchemy as sa
    from sqlalchemy.exc import DBAPIError
except ImportError:  # pragma: no cover
    sa = None  # type: ignore[assignment]
    DBAPIError = Exception  # type: ignore[assignment,misc]

# text() treats ":name" as a bind parameter; escape single colons, leave "::" casts alone.
_SINGLE_COLON = re.compile(r"(?<!:):(?!:)")


class PostgresDatabase:
    dialect = "PostgreSQL"

    def __init__(
        self,
        url: str,
        *,
        schema_name: str = "public",
        include_tables: list[str] | None = None,
        exclude_tables: list[str] | tuple[str, ...] = (),
    ):
        if sa is None:
            raise ImportError("PostgresDatabase needs SQLAlchemy: pip install 'sqlalchemy>=2.0' 'psycopg[binary]'")
        self.engine = sa.create_engine(url, pool_pre_ping=True)
        self.schema_name = schema_name
        self.allowed_schemas = (schema_name.lower(),)
        self._include = {t.lower() for t in include_tables} if include_tables else None
        self._exclude = {t.lower() for t in exclude_tables}
        self._schema: Schema | None = None

    @property
    def schema(self) -> Schema:
        if self._schema is None:
            self._schema = self._introspect()
        return self._schema

    def _introspect(self) -> Schema:
        insp = sa.inspect(self.engine)
        tables: list[Table] = []
        names = [(n, False) for n in insp.get_table_names(schema=self.schema_name)]
        names += [(n, True) for n in insp.get_view_names(schema=self.schema_name)]
        for name, is_view in sorted(names):
            low = name.lower()
            if low in self._exclude or (self._include is not None and low not in self._include):
                continue
            pk = set(insp.get_pk_constraint(name, schema=self.schema_name).get("constrained_columns") or [])
            columns = [
                Column(c["name"], str(c["type"]), not c.get("nullable", True), c["name"] in pk)
                for c in insp.get_columns(name, schema=self.schema_name)
            ]
            fks = []
            for fk in insp.get_foreign_keys(name, schema=self.schema_name):
                for local, remote in zip(fk["constrained_columns"], fk["referred_columns"]):
                    fks.append(ForeignKey(local, fk["referred_table"], remote))
            tables.append(Table(name, columns, fks, None, is_view))
        return Schema("PostgreSQL", tables)

    def execute_readonly(self, sql: str, *, max_rows: int = 200, timeout_s: float = 5.0) -> ExecResult:
        started = time.perf_counter()
        statement = sa.text(_SINGLE_COLON.sub(r"\\:", sql))
        try:
            with self.engine.connect() as conn:
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
                conn.execute(sa.text(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}"))
                result = conn.execute(statement)
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchmany(max_rows + 1)]
                conn.rollback()
        except DBAPIError as exc:
            message = str(getattr(exc, "orig", None) or exc).strip()
            if "statement timeout" in message.lower():
                raise QueryTimeout(f"Query exceeded the {timeout_s:g}s time limit") from exc
            raise QueryExecutionError(message) from exc
        truncated = len(rows) > max_rows
        return ExecResult(columns, rows[:max_rows], truncated, round((time.perf_counter() - started) * 1000, 1))
