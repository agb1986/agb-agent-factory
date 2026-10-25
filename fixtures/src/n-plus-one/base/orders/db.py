"""Order table access."""

import sqlite3


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open the database and create the tables when they are missing."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orders "
        "(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, total INTEGER NOT NULL)"
    )
    return conn


def add_customer(conn: sqlite3.Connection, name: str) -> int:
    """Insert a customer and return the new id."""
    cursor = conn.execute("INSERT INTO customers (name) VALUES (?)", (name,))
    conn.commit()
    return cursor.lastrowid or 0


def add_order(conn: sqlite3.Connection, customer_id: int, total: int) -> int:
    """Insert an order and return the new id."""
    cursor = conn.execute(
        "INSERT INTO orders (customer_id, total) VALUES (?, ?)", (customer_id, total)
    )
    conn.commit()
    return cursor.lastrowid or 0


def list_orders(conn: sqlite3.Connection) -> list[tuple[int, int, int]]:
    """Return every `(id, customer_id, total)` order row, oldest first."""
    return conn.execute("SELECT id, customer_id, total FROM orders ORDER BY id").fetchall()
