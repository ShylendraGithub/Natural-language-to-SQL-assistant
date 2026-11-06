"""A rule-based stand-in for the LLM so the project runs offline.

This is NOT an AI model. It pattern-matches a handful of canned questions against
the demo sales database and returns hand-written SQL. Its purposes:

* let you try the whole pipeline (validation, read-only execution, retry loop,
  audit log) with no API key and no network;
* make the safety layers demonstrable: ask it to "delete cancelled orders" and it
  will *comply*, exactly like a jailbroken or confused model could, so you can watch
  the validator and the database refuse;
* give the test-suite a deterministic model.

Dates are read from the "Date context" block of the system prompt, the same block a
real model sees.
"""

from __future__ import annotations

import re

_PERIODS = ["last quarter", "this quarter", "last month", "this month", "last year", "this year"]


def _period(question: str, system: str) -> tuple[str, str] | None:
    for name in _PERIODS:
        if name in question:
            label = name.capitalize()
            m = re.search(rf"^{label}: (\d{{4}}-\d{{2}}-\d{{2}}) <= date < (\d{{4}}-\d{{2}}-\d{{2}})", system, re.M)
            if m:
                return m.group(1), m.group(2)
    return None


def _fence(sql: str) -> str:
    return f"```sql\n{sql.strip()}\n```"


class DemoLLM:
    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        question = messages[0]["content"].removeprefix("Question:").strip().lower()
        is_retry = len(messages) > 1
        period = _period(question, system)
        order_filter = (
            f"\n  AND o.order_date >= '{period[0]}' AND o.order_date < '{period[1]}'" if period else ""
        )

        if re.search(r"\b(delete|remove|wipe|drop|truncate)\b", question):
            # Simulates a model that goes along with a destructive request.
            return _fence("DELETE FROM orders WHERE status = 'cancelled'")

        if re.search(r"\btop\b.*\bcustomers?\b.*\brevenue\b|\bcustomers?\b.*\bby revenue\b", question):
            m = re.search(r"top\s+(\d+)", question)
            n = int(m.group(1)) if m else 5
            return _fence(
                f"""
SELECT c.customer_id, c.name, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue
FROM customers AS c
JOIN orders AS o ON o.customer_id = c.customer_id
JOIN order_items AS oi ON oi.order_id = o.order_id
WHERE o.status = 'completed'{order_filter}
GROUP BY c.customer_id, c.name
ORDER BY revenue DESC, c.customer_id
LIMIT {n}"""
            )

        if "revenue" in question and "category" in question:
            return _fence(
                f"""
SELECT p.category, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue
FROM order_items AS oi
JOIN orders AS o ON o.order_id = oi.order_id
JOIN products AS p ON p.product_id = oi.product_id
WHERE o.status = 'completed'{order_filter}
GROUP BY p.category
ORDER BY revenue DESC"""
            )

        if "orders" in question and re.search(r"per month|monthly|each month", question):
            where = f"\nWHERE order_date >= '{period[0]}' AND order_date < '{period[1]}'" if period else ""
            return _fence(
                f"""
SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS orders
FROM orders{where}
GROUP BY month
ORDER BY month"""
            )

        if "customers" in question and "country" in question:
            # First attempt uses a column that does not exist, so the retry loop can be demonstrated.
            column = "country" if is_retry else "nation"
            return _fence(
                f"""
SELECT {column} AS country, COUNT(*) AS customers
FROM customers
GROUP BY {column}
ORDER BY customers DESC"""
            )

        if re.search(r"how many customers|number of customers|count of customers", question):
            return _fence("SELECT COUNT(*) AS customer_count FROM customers")

        return "CANNOT_ANSWER: the offline demo model only understands a few canned questions"
