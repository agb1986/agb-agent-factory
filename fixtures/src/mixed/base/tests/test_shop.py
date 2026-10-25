from shop.format import format_price
from shop.shipping import shipping_cost


def test_small_order_pays_shipping() -> None:
    assert shipping_cost(1000) == 499


def test_large_order_ships_free() -> None:
    assert shipping_cost(9000) == 0


def test_format_price() -> None:
    assert format_price(1250) == "12.50"
    assert format_price(5) == "0.05"
