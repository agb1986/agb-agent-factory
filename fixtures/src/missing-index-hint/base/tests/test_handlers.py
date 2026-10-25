from notifications.db import add_notification, connect
from notifications.handlers import handle_get_notification


def test_get_notification_returns_the_body() -> None:
    conn = connect()
    first = add_notification(conn, 1, "welcome")
    assert handle_get_notification(conn, first) == "welcome"


def test_get_notification_is_none_when_missing() -> None:
    assert handle_get_notification(connect(), 99) is None
