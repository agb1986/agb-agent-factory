"""Request handlers for the notifications API."""

import sqlite3


def handle_get_notification(conn: sqlite3.Connection, notification_id: int) -> str | None:
    """GET /notifications/<id>: return the body of one notification, or None when it is missing."""
    row = conn.execute("SELECT body FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    return row[0] if row else None


def handle_unread(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """GET /notifications/unread: return the bodies of the user's unread notifications.

    The web client calls this on every page load, and the table holds millions of rows.
    """
    rows = conn.execute(
        "SELECT body FROM notifications WHERE user_id = ? AND read_at IS NULL ORDER BY id",
        (user_id,),
    ).fetchall()
    return [body for (body,) in rows]
