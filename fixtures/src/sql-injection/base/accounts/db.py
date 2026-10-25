"""User table access."""

import sqlite3


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open the database and create the users table when it is missing."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users "
        "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)"
    )
    return conn


def add_user(conn: sqlite3.Connection, name: str, email: str) -> int:
    """Insert a user and return the new id."""
    cursor = conn.execute("INSERT INTO users (name, email) VALUES (?, ?)", (name, email))
    conn.commit()
    return cursor.lastrowid or 0


def get_user(conn: sqlite3.Connection, user_id: int) -> tuple[int, str, str] | None:
    """Return the `(id, name, email)` row for `user_id`, or None."""
    return conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
