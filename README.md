# NL → SQL Assistant

Ask a database questions in plain English. An LLM writes the SQL, and a stack of guardrails
makes sure that **whatever the model writes, it can only ever read**.

```
$ python -m nl2sql --demo "top 5 customers by revenue last quarter"

SQL:
  SELECT c.customer_id, c.name, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue
  FROM customers AS c
  JOIN orders AS o ON o.customer_id = c.customer_id
  JOIN order_items AS oi ON oi.order_id = o.order_id
  WHERE o.status = 'completed'
    AND o.order_date >= '2026-07-01' AND o.order_date < '2026-10-01'
  GROUP BY c.customer_id, c.name
  ORDER BY revenue DESC, c.customer_id
  LIMIT 5

customer_id  name            revenue
-----------  ------------  ---------
          9  Diya Clark    21,239.37
         10  Zoe Das       12,228.50
         22  Isha Nair      5,997.50
         19  Arjun Muller   3,711.50
         41  Ethan Menon    3,675.00

$ python -m nl2sql --demo "delete all cancelled orders"
SQL:
  DELETE FROM orders WHERE status = 'cancelled'

BLOCKED: Only SELECT queries are allowed (query starts with 'DELETE')
```

(`--demo` uses an offline rule-based stand-in for the model, see [Demo mode](#demo-mode).)

## How it works

```
 question ──► prompt builder ──► LLM ──► parse reply ──► static validator ──► read-only DB ──► rows
                │                          │                    │                  │
   live schema (introspected)     CANNOT_ANSWER?        "unsafe": stop,     DB errors / timeouts
   date ranges (computed)         → declined            never retry         → fed back to the LLM,
   business definitions                                 "invalid": retry    one bounded retry
                                                                            every request → audit log
```

### Defence in depth

The prompt is *not* a security control; a model can be talked out of its instructions.
Safety comes from layers that don't depend on the model behaving:

| # | Layer | Where | Stops |
|---|-------|-------|-------|
| 1 | Static validator (tokenizer-based) | `validator.py` | multiple statements, non-SELECT, DDL/DML/admin keywords, unknown tables, `sqlite_*` catalogs, foreign schemas, odd Unicode whitespace |
| 2 | Read-only connection (`mode=ro`) | `executor.py` | any write at the file level |
| 3 | `PRAGMA query_only = ON` | `executor.py` | any write at the SQL level |
| 4 | SQLite **authorizer** callback | `executor.py` | everything except SELECT: DDL, `ATTACH`, `PRAGMA`, `load_extension`, and reads of tables you've hidden (even `COUNT(*)` on them) |
| 5 | Timeout, row cap, value-size cap | `executor.py` | runaway recursive CTEs, huge result sets, `randomblob(1e9)` memory bombs |
| 6 | Audit log (JSONL) | `assistant.py` | nothing, but every question, SQL and verdict is recorded (never result rows) |

Layers 2–5 are tested **without** the validator (`tests/test_executor.py`), so the database
refuses harm even if layer 1 has a bug.

For PostgreSQL the equivalent is a least-privilege role, `SET TRANSACTION READ ONLY` and
`statement_timeout`: see `docs/postgres_readonly_role.sql`.

### Handling known LLM weaknesses

| Weakness | What this project does |
|----------|------------------------|
| Bad date arithmetic ("last quarter") | Python computes the ranges; they are put in the prompt as `Last quarter: 2026-07-01 <= date < 2026-10-01`. Tested across year boundaries. |
| Hallucinated tables/columns | Schema is introspected live (never hand-written). Errors go back to the model once, with the list of real tables. |
| Ambiguous business terms ("revenue") | A glossary of definitions goes in the prompt (`--glossary`). Here: revenue = completed orders only. |
| Questions the data can't answer | The model has an explicit `CANNOT_ANSWER:` escape hatch, reported as *unanswerable* rather than guessed. |
| Prompt injection via the question | Treated as untrusted; the validator and the DB layers don't care what the model was told. |
| Data leaving your network | The LLM sees schema + low-cardinality values (e.g. `status`), never row data. `--no-value-hints` turns even that off; `--exclude-tables` hides tables entirely. |

## Quick start

Needs Python 3.10+ (3.11+ for the value-size cap). The core has **no third-party dependencies**.

```bash
# 1. Offline demo, no API key, no network
python -m nl2sql --demo "top 5 customers by revenue last quarter"
python -m nl2sql --demo                      # interactive prompt

# 2. With a real model
cp .env.example .env                         # add ANTHROPIC_API_KEY
python -m nl2sql --db path/to/your.db "top 5 customers by revenue last quarter"
python -m nl2sql --db your.db --dry-run "..."          # show the SQL, don't run it
python -m nl2sql --db your.db --show-schema            # exactly what the model sees
python -m nl2sql --db your.db --exclude-tables salaries,users --audit-log audit.jsonl "..."

# 3. As a library
python - <<'EOF'
from nl2sql import NL2SQLAssistant, SQLiteDatabase, AnthropicClient
a = NL2SQLAssistant(SQLiteDatabase("your.db"), AnthropicClient(), glossary=["revenue = ..."])
r = a.ask("top 5 customers by revenue last quarter")
print(r.status, r.sql, r.rows)
EOF
```

`AnthropicClient` speaks the Anthropic Messages API over plain HTTPS (standard library only).
The model defaults to `claude-sonnet-5-5`; override with `--model` or `NL2SQL_MODEL`.
Any other provider only needs one method: `complete(system, messages) -> str`.

### PostgreSQL

```bash
pip install "sqlalchemy>=2.0" "psycopg[binary]"
psql -f docs/postgres_readonly_role.sql                # creates a least-privilege role
python -m nl2sql --db "postgresql+psycopg://nl2sql_reader:change-me@localhost/mydb" "..."
```

> **Status:** `nl2sql/pg_backend.py` is a reference implementation that has **not been run**
> (it was written in an environment without SQLAlchemy or a Postgres server). Try it on a scratch
> database first. The SQLite backend is the fully tested one.

### Demo mode

`--demo` seeds a sample sales database (60 customers, 25 products, 1,500 orders, dates relative to
today) and uses `DemoLLM`, a **rule-based stand-in, not an AI model**. It recognises a few canned
questions so you can exercise the whole pipeline offline. It deliberately *complies* with
"delete all cancelled orders", exactly as a tricked model might, so you can watch the guardrails
refuse. It also uses a wrong column name on "customers per country" to show the self-correction
loop (`5 row(s), 1 retry`).

## Evaluating a real model

```bash
python -m nl2sql.evaluate --db demo_sales.db        # needs ANTHROPIC_API_KEY
```

`eval/questions.json` holds 9 answerable questions with hand-written gold SQL plus 3 requests
that must be refused. The harness reports **execution accuracy** (same rows as the gold query,
order-insensitive) and how many unsafe requests were handled. Add questions for your own schema,
and run it each time you change the prompt, the model, or the glossary. Passing `--demo` only
smoke-tests the harness (that model knows a handful of canned questions, so its score means nothing).

## Tests

```bash
pip install pytest && python -m pytest        # 131 tests, ~2 s
```

* validator: safe/unsafe/malformed corpora, smuggling tricks (comments, `;`, quoted catalog names, Unicode whitespace)
* executor: writes/DDL/`ATTACH`/`PRAGMA`/hidden-table reads blocked *without* the validator; timeout; row cap; value-size cap
* assistant: retry loop, unsafe-never-retried, give-up, declined questions, audit log contains no row data
* end to end: "top 5 customers by revenue last quarter" is recomputed in plain Python and compared with the pipeline's answer
* LLM client: request/response shape verified against a local stub server (no live API call is made in tests)

## Project layout

```
nl2sql/
  schema.py       live introspection → compact DDL for the prompt (+ allow/deny lists, value hints)
  prompts.py      system prompt, date-range maths, reply parsing
  validator.py    tokenizer-based static SQL checks
  executor.py     SQLite read-only execution: authorizer, timeout, caps
  assistant.py    the pipeline: generate → validate → execute → retry, audit log
  llm.py          LLM protocol + Anthropic HTTP client
  pg_backend.py   optional PostgreSQL/SQLAlchemy backend (untested reference)
  seed.py         deterministic sample database
  demo_llm.py     offline rule-based stand-in for the model
  evaluate.py     execution-accuracy harness
  cli.py          command line + interactive prompt
tests/            pytest suite        eval/   gold questions        docs/   Postgres role
```

## Known limitations

* The validator is a pre-filter, not a full SQL parser. It checks table references after
  `FROM`/`JOIN` and won't catch every exotic construct. That is why layers 2–5 exist.
* Columns are not validated statically; a wrong column is caught by the database and fed back for one retry.
* No row-level or column-level permissions in SQLite. On Postgres use the role/RLS ideas in `docs/`.
* Results are capped (200 rows by default); questions needing more should be aggregated.
* Accuracy on your data depends on the model, the schema's naming, and the glossary. Measure it with the eval harness.

## Resume bullets

Fill the brackets with **your own measured numbers** (run `nl2sql.evaluate` against your data and a
real model; don't quote a figure you haven't measured):

* Built a natural-language-to-SQL assistant in Python (SQLite/PostgreSQL, LLM API) that introspects the live
  schema, resolves relative dates in code, and self-corrects from database errors; **[X]%** execution
  accuracy on a **[N]**-question evaluation set.
* Designed a six-layer safety stack (tokenizer-based SQL validator, read-only connections, SQLite authorizer
  callbacks, timeouts and row/size caps, audit logging) so model output can never modify data; verified by
  a 131-test suite that attacks each layer in isolation.
* Wrote least-privilege PostgreSQL role setup (SELECT grants, read-only transactions, statement timeouts)
  and an evaluation harness that tracks accuracy and unsafe-request handling across prompt/model changes.

**Interview talking points:** why prompt instructions aren't a security boundary; why the authorizer
beats keyword filtering; the `COUNT(*)` row-count leak the authorizer had to close; why unsafe SQL is never
retried but a hallucinated column is; privacy trade-offs of sending schema/values to an LLM provider.
