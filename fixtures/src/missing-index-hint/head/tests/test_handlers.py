from notifications.db import add_notification, connect
from notifications.handlers import handle_get_notification, handle_unread


def test_get_notification_returns_the_body() -> None:
    conn = connect()
    first = add_notification(conn, 1, "welcome")
    assert handle_get_notification(conn, first) == "welcome"


def test_get_notification_is_none_when_missing() -> None:
    assert handle_get_notification(connect(), 99) is None


def test_unread_lists_only_the_users_unread_notifications() -> None:
    conn = connect()
    add_notification(conn, 1, "welcome")
    add_notification(conn, 2, "other user")
    read = add_notification(conn, 1, "already read")
    conn.execute("UPDATE notifications SET read_at = '2026-01-01' WHERE id = ?", (read,))
    add_notification(conn, 1, "new follower")
    assert handle_unread(conn, 1) == ["welcome", "new follower"]
