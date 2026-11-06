"""Deterministic sample database (customers / products / orders / order_items).

Dates are generated relative to ``today`` so questions like "last quarter" always
have data to find. Same ``seed`` + same ``today`` => identical database.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DEMO_GLOSSARY = [
    "revenue = SUM(order_items.quantity * order_items.unit_price) over orders whose status = 'completed' "
    "(cancelled and refunded orders never count as revenue)",
    "an order's date is orders.order_date; a customer's signup date is customers.signup_date",
    "order_items.unit_price is the price actually charged, which can differ from products.unit_price",
]

SCHEMA_SQL = """
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    city        TEXT NOT NULL,
    country     TEXT NOT NULL,
    signup_date TEXT NOT NULL
);
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('completed', 'cancelled', 'refunded'))
);
CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    unit_price    REAL NOT NULL
);
CREATE INDEX idx_orders_date      ON orders(order_date);
CREATE INDEX idx_orders_customer  ON orders(customer_id);
CREATE INDEX idx_items_order      ON order_items(order_id);
CREATE INDEX idx_items_product    ON order_items(product_id);
"""

_FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Vikram", "Meera", "Arjun", "Isha", "Kabir", "Sneha",
          "Liam", "Emma", "Noah", "Olivia", "Lucas", "Mia", "Ethan", "Zoe", "Hans", "Greta"]
_LAST = ["Sharma", "Reddy", "Iyer", "Patel", "Nair", "Gupta", "Rao", "Menon", "Singh", "Das",
         "Smith", "Brown", "Taylor", "Wilson", "Clark", "Muller", "Schmidt", "Tan", "Lim", "Khan"]
_CITIES = {
    "India": ["Bengaluru", "Hyderabad", "Mumbai", "Chennai"],
    "USA": ["Austin", "Seattle", "Boston"],
    "UK": ["London", "Manchester"],
    "Germany": ["Berlin", "Munich"],
    "Singapore": ["Singapore"],
}
_PRODUCTS = [
    ("Wireless Mouse", "Electronics", 24.99), ("Mechanical Keyboard", "Electronics", 89.0),
    ("USB-C Hub", "Electronics", 39.5), ("Noise-Cancelling Headphones", "Electronics", 199.0),
    ("Webcam HD", "Electronics", 59.0), ("Python Cookbook", "Books", 45.0),
    ("Database Internals", "Books", 52.0), ("Deep Learning Handbook", "Books", 68.0),
    ("SQL Performance Explained", "Books", 39.0), ("Statistics Primer", "Books", 29.0),
    ("Desk Lamp", "Home", 34.0), ("Standing Desk Mat", "Home", 49.0),
    ("Ergonomic Chair", "Home", 259.0), ("Bookshelf", "Home", 120.0),
    ("Coffee Maker", "Home", 79.0), ("Yoga Mat", "Fitness", 22.0),
    ("Dumbbell Set", "Fitness", 74.0), ("Resistance Bands", "Fitness", 15.0),
    ("Water Bottle", "Fitness", 12.5), ("Running Belt", "Fitness", 18.0),
    ("Notebook Pack", "Stationery", 9.0), ("Gel Pens", "Stationery", 7.5),
    ("Sticky Notes", "Stationery", 4.0), ("Whiteboard", "Stationery", 42.0),
    ("Planner 2026", "Stationery", 16.0),
]


def seed_database(
    path: str | Path,
    *,
    today: date | None = None,
    seed: int = 42,
    n_customers: int = 60,
    n_orders: int = 1500,
    overwrite: bool = False,
) -> Path:
    path = Path(path)
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"{path} already exists (pass overwrite=True to replace it)")
        path.unlink()
    today = today or date.today()
    rng = random.Random(seed)

    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_SQL)

        customers = []
        for cid in range(1, n_customers + 1):
            first, last = rng.choice(_FIRST), rng.choice(_LAST)
            country = rng.choices(list(_CITIES), weights=[5, 3, 2, 2, 1])[0]
            signup = today - timedelta(days=rng.randint(60, 900))
            customers.append(
                (cid, f"{first} {last}", f"{first}.{last}.{cid}@example.com".lower(),
                 rng.choice(_CITIES[country]), country, signup.isoformat())
            )
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", customers)

        conn.executemany(
            "INSERT INTO products VALUES (?,?,?,?)",
            [(i, n, c, p) for i, (n, c, p) in enumerate(_PRODUCTS, start=1)],
        )

        # A few customers order far more often than the rest, so rankings are meaningful.
        weights = [rng.paretovariate(1.6) for _ in range(n_customers)]
        item_id = 1
        for oid in range(1, n_orders + 1):
            cid = rng.choices(range(1, n_customers + 1), weights=weights)[0]
            order_date = today - timedelta(days=rng.randint(1, 540))
            status = rng.choices(["completed", "cancelled", "refunded"], weights=[85, 10, 5])[0]
            conn.execute("INSERT INTO orders VALUES (?,?,?,?)", (oid, cid, order_date.isoformat(), status))
            for pid in rng.sample(range(1, len(_PRODUCTS) + 1), rng.randint(1, 4)):
                price = _PRODUCTS[pid - 1][2]
                conn.execute(
                    "INSERT INTO order_items VALUES (?,?,?,?,?)",
                    (item_id, oid, pid, rng.randint(1, 5), price),
                )
                item_id += 1
        conn.commit()
    finally:
        conn.close()
    return path
