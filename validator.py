"""Static validation of LLM-generated SQL (layer 1 of the safety stack).

This is a small tokenizer-based checker, deliberately not a regex over the raw
string: string literals, quoted identifiers and comments are tokenized first, so
``WHERE note = 'DROP TABLE x'`` is fine while a real ``DROP`` is not.

Checks, in order:
  * exactly one statement, and it starts with SELECT or WITH
  * no write/DDL/admin keywords anywhere outside literals
  * every table referenced is on the allow-list (CTE names excepted)
  * no system catalogs (sqlite_*, pg_*) and no foreign schemas

It is a *pre-filter*, not the security boundary. The database-level controls in
``executor.py`` (read-only connection + authorizer) still apply to whatever passes.
Error ``kind`` tells the caller whether retrying makes sense:
  * ``unsafe``  - the query tried to do something it must never do; don't retry
  * ``invalid`` - the query is malformed or uses unknown names; the LLM may fix it
"""

from __future__ import annotations

from dataclasses import dataclass


class SQLValidationError(Exception):
    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str  # comments and trailing semicolon removed
    tables: tuple[str, ...]  # real tables referenced (lower-case, sorted)


FORBIDDEN_KEYWORDS = frozenset(
    {
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
        "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX", "ANALYZE", "GRANT", "REVOKE",
        "COPY", "CALL", "EXEC", "EXECUTE", "MERGE", "BEGIN", "COMMIT", "ROLLBACK",
        "SAVEPOINT", "RELEASE", "INTO", "LOAD_EXTENSION",
    }
)

# Words that end a table reference, so they are never mistaken for an alias.
_CLAUSE_WORDS = frozenset(
    {
        "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT",
        "ON", "USING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "OUTER",
        "WINDOW", "SELECT", "FROM", "SET", "VALUES", "RETURNING", "LATERAL",
    }
)

# FROM inside these function calls is not a table reference: EXTRACT(year FROM d)
_FROM_IN_FUNCTION = frozenset({"EXTRACT", "SUBSTRING", "TRIM", "OVERLAY", "POSITION"})


@dataclass(frozen=True)
class Token:
    kind: str  # word | string | qident | number | punct
    text: str
    start: int
    end: int

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""

    def is_punct(self, ch: str) -> bool:
        return self.kind == "punct" and self.text == ch


def _scan_quoted(sql: str, i: int, quote: str) -> int:
    """Return the index just past the closing quote (doubled quotes escape)."""
    j = i + 1
    n = len(sql)
    while True:
        k = sql.find(quote, j)
        if k == -1:
            raise SQLValidationError("Unterminated quoted string or identifier", "invalid")
        if k + 1 < n and sql[k + 1] == quote:
            j = k + 2
            continue
        return k + 1


def _tokenize(sql: str) -> tuple[list[Token], list[tuple[int, int]]]:
    tokens: list[Token] = []
    comments: list[tuple[int, int]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\n\r\f\v":
            i += 1
        elif c.isspace():
            # Unicode whitespace (NBSP, U+2028, ...) is not whitespace to SQLite. Refusing it keeps
            # this tokenizer and the database engine from disagreeing about where tokens end.
            raise SQLValidationError(f"The query contains a non-standard whitespace character (U+{ord(c):04X})", "invalid")
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            comments.append((i, j))
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                raise SQLValidationError("Unterminated block comment", "invalid")
            comments.append((i, j + 2))
            i = j + 2
        elif c == "'":
            j = _scan_quoted(sql, i, "'")
            tokens.append(Token("string", sql[i:j], i, j))
            i = j
        elif c in ('"', "`"):
            j = _scan_quoted(sql, i, c)
            tokens.append(Token("qident", sql[i:j], i, j))
            i = j
        elif c == "[":
            j = sql.find("]", i + 1)
            if j == -1:
                raise SQLValidationError("Unterminated [bracketed] identifier", "invalid")
            tokens.append(Token("qident", sql[i : j + 1], i, j + 1))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            tokens.append(Token("word", sql[i:j], i, j))
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            tokens.append(Token("number", sql[i:j], i, j))
            i = j
        else:
            tokens.append(Token("punct", c, i, i + 1))
            i += 1
    return tokens, comments


def _ident(tok: Token) -> str:
    """Normalised identifier text (quotes removed, lower-cased)."""
    if tok.kind == "qident":
        q = tok.text[0]
        if q == "[":
            return tok.text[1:-1].lower()
        return tok.text[1:-1].replace(q + q, q).lower()
    return tok.text.lower()


def _skip_parens(tokens: list[Token], i: int) -> int:
    """tokens[i] is '('; return the index just after its matching ')'."""
    depth = 0
    n = len(tokens)
    while i < n:
        if tokens[i].is_punct("("):
            depth += 1
        elif tokens[i].is_punct(")"):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _cte_names(tokens: list[Token]) -> set[str]:
    names: set[str] = set()
    n = len(tokens)
    for i, t in enumerate(tokens):
        if t.upper != "WITH":
            continue
        j = i + 1
        if j < n and tokens[j].upper == "RECURSIVE":
            j += 1
        while j < n and tokens[j].kind in ("word", "qident"):
            names.add(_ident(tokens[j]))
            j += 1
            if j < n and tokens[j].is_punct("("):  # column list
                j = _skip_parens(tokens, j)
            if j < n and tokens[j].upper == "AS":
                j += 1
            else:
                break
            if j < n and tokens[j].upper == "NOT":
                j += 1
            if j < n and tokens[j].upper == "MATERIALIZED":
                j += 1
            if j < n and tokens[j].is_punct("("):
                j = _skip_parens(tokens, j)
            else:
                break
            if j < n and tokens[j].is_punct(","):
                j += 1
                continue
            break
    return names


def _read_name(tokens: list[Token], i: int) -> tuple[str | None, str, int] | None:
    n = len(tokens)
    if i >= n or tokens[i].kind not in ("word", "qident"):
        return None
    parts = [_ident(tokens[i])]
    i += 1
    while i + 1 < n and tokens[i].is_punct(".") and tokens[i + 1].kind in ("word", "qident"):
        parts.append(_ident(tokens[i + 1]))
        i += 2
    schema = parts[-2] if len(parts) >= 2 else None
    return schema, parts[-1], i


def _table_refs(tokens: list[Token]) -> list[tuple[str | None, str]]:
    """Find (schema, table) pairs that follow FROM / JOIN."""
    refs: list[tuple[str | None, str]] = []
    n = len(tokens)
    paren_stack: list[str] = []
    i = 0
    while i < n:
        t = tokens[i]
        if t.is_punct("("):
            paren_stack.append(tokens[i - 1].upper if i > 0 else "")
            i += 1
            continue
        if t.is_punct(")"):
            if paren_stack:
                paren_stack.pop()
            i += 1
            continue
        if t.upper in ("FROM", "JOIN"):
            if t.upper == "FROM" and (
                (paren_stack and paren_stack[-1] in _FROM_IN_FUNCTION)
                or (i > 0 and tokens[i - 1].upper == "DISTINCT")
            ):
                i += 1
                continue
            is_from = t.upper == "FROM"
            i += 1
            while True:
                if i >= n or tokens[i].is_punct("("):
                    break  # subquery: its own FROM is found by the outer scan
                parsed = _read_name(tokens, i)
                if parsed is None:
                    break
                schema, name, i = parsed
                refs.append((schema, name))
                if i < n and tokens[i].upper == "AS":
                    i += 2
                elif i < n and tokens[i].kind in ("word", "qident") and tokens[i].upper not in _CLAUSE_WORDS:
                    i += 1
                if is_from and i < n and tokens[i].is_punct(","):
                    i += 1
                    continue
                break
            continue
        i += 1
    return refs


def validate_sql(
    sql: str,
    allowed_tables: list[str] | tuple[str, ...] | set[str],
    *,
    allowed_schemas: tuple[str, ...] = ("main",),
    max_length: int = 10_000,
) -> ValidatedSQL:
    """Validate ``sql`` or raise :class:`SQLValidationError`."""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLValidationError("The query is empty", "invalid")
    if len(sql) > max_length:
        raise SQLValidationError(f"The query is longer than {max_length} characters", "invalid")
    if "\x00" in sql:
        raise SQLValidationError("The query contains a NUL byte", "unsafe")

    tokens, comments = _tokenize(sql)

    # Replace comments with spaces (same length, so token offsets stay valid).
    chars = list(sql)
    for a, b in comments:
        chars[a:b] = " " * (b - a)
    cleaned = "".join(chars)

    semis = [k for k, t in enumerate(tokens) if t.is_punct(";")]
    if semis:
        if semis != [len(tokens) - 1]:
            raise SQLValidationError("Multiple statements are not allowed", "unsafe")
        cleaned = cleaned[: tokens[-1].start]
        tokens = tokens[:-1]
    cleaned = cleaned.strip()

    if not tokens:
        raise SQLValidationError("The query is empty", "invalid")

    first = tokens[0]
    if first.upper not in ("SELECT", "WITH"):
        raise SQLValidationError(f"Only SELECT queries are allowed (query starts with {first.text!r})", "unsafe")

    for t in tokens:
        if t.upper in FORBIDDEN_KEYWORDS:
            raise SQLValidationError(f"Forbidden keyword: {t.upper}", "unsafe")

    allowed = {name.lower() for name in allowed_tables}
    schemas = {s.lower() for s in allowed_schemas}
    ctes = _cte_names(tokens)
    used: set[str] = set()
    for schema, name in _table_refs(tokens):
        if schema is not None and schema not in schemas:
            raise SQLValidationError(f"Access to schema {schema!r} is not allowed", "unsafe")
        if schema is None and name in ctes:
            continue
        if name.startswith(("sqlite_", "pg_")) or name == "information_schema":
            raise SQLValidationError(f"System table {name!r} is off limits", "unsafe")
        if name not in allowed:
            raise SQLValidationError(
                f"Unknown table {name!r}. Available tables: {', '.join(sorted(allowed))}", "invalid"
            )
        used.add(name)

    return ValidatedSQL(cleaned, tuple(sorted(used)))
