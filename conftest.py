import datetime as dt

import pytest

from nl2sql.executor import SQLiteDatabase
from nl2sql.seed import seed_database

TODAY = dt.date(2026, 11, 6)  # fixed, so "last quarter" is always 2026-07-01 .. 2026-09-30


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "sales.db"
    seed_database(path, today=TODAY)
    return path


@pytest.fixture
def db(db_path):
    return SQLiteDatabase(db_path)


class ScriptedLLM:
    """Returns pre-written replies in order and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append((system, [dict(m) for m in messages]))
        return self.replies.pop(0)


def fence(sql: str) -> str:
    return f"```sql\n{sql}\n```"
