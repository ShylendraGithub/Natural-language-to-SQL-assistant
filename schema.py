"""Schema introspection.

Reads table/column/foreign-key metadata straight from the database catalog so the
LLM prompt always matches the real schema (no hand-maintained DDL to drift).

Privacy note: low-cardinality text columns (e.g. ``status``) get their distinct
values added to the prompt as hints, because the model otherwise has to guess
literals like 'completed' vs 'complete'. Those values are sent to the LLM
provider. Turn it off with ``value_hints=False`` if that is not acceptable.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

HINT_MAX_DISTINCT = 8
HINT_MIN_ROWS = 20
HINT_MAX_LEN = 40


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _display(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_]\w*", name) else quote_ident(name)


@dataclass
class Column:
    name: str
    type: str
    notnull: bool = False
    primary_key: bool = False
    hints: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str | None = None


@dataclass
class Table:
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    row_count: int | None = None
    is_view: bool = False


@dataclass
class Schema:
    dialect: str
    tables: list[Table]

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def to_prompt(self) -> str:
        """Render the schema as compact DDL-like text for the LLM."""
        blocks: list[str] = []
        for t in self.tables:
            items: list[tuple[str, str | None]] = []
            for c in t.columns:
                parts = [_display(c.name), c.type or "ANY"]
                if c.primary_key:
                    parts.append("PRIMARY KEY")
                elif c.notnull:
                    parts.append("NOT NULL")
                comment = "values: " + ", ".join(c.hints) if c.hints else None
                items.append((" ".join(parts), comment))
            for fk in t.foreign_keys:
                ref = _display(fk.ref_table)
                if fk.ref_column:
                    ref += f"({_display(fk.ref_column)})"
                items.append((f"FOREIGN KEY ({_display(fk.column)}) REFERENCES {ref}", None))

            kind = "VIEW" if t.is_view else "TABLE"
            header = f"{kind} {_display(t.name)} ("
            if t.row_count is not None:
                header += f"  -- {t.row_count:,} rows"
            lines = [header]
            for i, (text, comment) in enumerate(items):
                comma = "," if i < len(items) - 1 else ""
                line = f"  {text}{comma}"
                if comment:
                    line += f"  -- {comment}"
                lines.append(line)
            lines.append(")")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def _is_texty(type_name: str) -> bool:
    t = (type_name or "").upper()
    return any(k in t for k in ("CHAR", "TEXT", "CLOB"))


def _sql_literal(value: str) -> str:
    return "'" + value[:HINT_MAX_LEN].replace("'", "''") + "'"


def introspect_sqlite(
    path: str | Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | tuple[str, ...] = (),
    value_hints: bool = True,
) -> Schema:
    """Introspect a SQLite file (opened read-only).

    ``include`` / ``exclude`` control which tables the assistant may see. Hiding a
    table here also hides it from the validator and the engine-level authorizer.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        objects = conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
        ).fetchall()
        inc = {n.lower() for n in include} if include else None
        exc = {n.lower() for n in exclude}
        tables: list[Table] = []
        for name, kind in objects:
            low = name.lower()
            if low.startswith("sqlite_") or low in exc:
                continue
            if inc is not None and low not in inc:
                continue
            tables.append(_introspect_table(conn, name, kind == "view", value_hints))
        return Schema("SQLite", tables)
    finally:
        conn.close()


def _introspect_table(conn: sqlite3.Connection, name: str, is_view: bool, value_hints: bool) -> Table:
    qt = quote_ident(name)
    info = conn.execute(f"PRAGMA table_info({qt})").fetchall()  # cid, name, type, notnull, dflt, pk
    fks = conn.execute(f"PRAGMA foreign_key_list({qt})").fetchall()  # id, seq, table, from, to, ...
    row_count = conn.execute(f"SELECT COUNT(*) FROM {qt}").fetchone()[0] if value_hints else None

    columns: list[Column] = []
    for _cid, cname, ctype, notnull, _default, pk in info:
        col = Column(cname, ctype or "", bool(notnull), bool(pk))
        if value_hints and row_count is not None and row_count >= HINT_MIN_ROWS and not pk and _is_texty(ctype):
            qc = quote_ident(cname)
            values = [
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT {qc} FROM {qt} WHERE {qc} IS NOT NULL LIMIT {HINT_MAX_DISTINCT + 1}"
                )
            ]
            if 0 < len(values) <= HINT_MAX_DISTINCT and all(isinstance(v, str) for v in values):
                col.hints = [_sql_literal(v) for v in sorted(values)]
        columns.append(col)

    foreign_keys = [ForeignKey(fk[3], fk[2], fk[4]) for fk in fks]
    return Table(name, columns, foreign_keys, row_count, is_view)
