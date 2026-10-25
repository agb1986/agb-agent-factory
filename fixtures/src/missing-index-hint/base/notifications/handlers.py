"""Request handlers for the notifications API."""

import sqlite3


def handle_get_notification(conn: sqlite3.Connection, notification_id: int) -> str | None:
    """GET /notifications/<id>: return the body of one notification, or None when it is missing."""
    row = conn.execute("SELECT body FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    return row[0] if row else None
