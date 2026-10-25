from orders.db import add_customer, add_order, connect, list_orders


def test_list_orders_is_oldest_first() -> None:
    conn = connect()
    customer = add_customer(conn, "ada")
    first = add_order(conn, customer, 100)
    second = add_order(conn, customer, 250)
    assert list_orders(conn) == [(first, customer, 100), (second, customer, 250)]
