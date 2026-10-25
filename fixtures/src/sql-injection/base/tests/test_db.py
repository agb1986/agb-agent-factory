from accounts.db import add_user, connect, get_user


def test_add_then_get() -> None:
    conn = connect()
    user_id = add_user(conn, "ada", "ada@example.com")
    assert get_user(conn, user_id) == (user_id, "ada", "ada@example.com")


def test_missing_user_is_none() -> None:
    assert get_user(connect(), 99) is None
