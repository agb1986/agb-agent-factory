from orders.db import add_customer, add_order, connect, list_orders, orders_with_customer_names


def test_list_orders_is_oldest_first() -> None:
    conn = connect()
    customer = add_customer(conn, "ada")
    first = add_order(conn, customer, 100)
    second = add_order(conn, customer, 250)
    assert list_orders(conn) == [(first, customer, 100), (second, customer, 250)]


def test_orders_with_customer_names() -> None:
    conn = connect()
    ada = add_customer(conn, "ada")
    grace = add_customer(conn, "grace")
    first = add_order(conn, ada, 100)
    second = add_order(conn, grace, 250)
    assert orders_with_customer_names(conn) == [(first, "ada", 100), (second, "grace", 250)]
