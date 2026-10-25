"""Database setup for notifications."""

import sqlite3
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open the database and apply every migration in file-name order."""
    conn = sqlite3.connect(path)
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        conn.executescript(migration.read_text(encoding="utf-8"))
    return conn


def add_notification(conn: sqlite3.Connection, user_id: int, body: str) -> int:
    """Insert an unread notification and return its id."""
    cursor = conn.execute(
        "INSERT INTO notifications (user_id, body) VALUES (?, ?)", (user_id, body)
    )
    conn.commit()
    return cursor.lastrowid or 0
