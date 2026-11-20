from storefront.api import checkout, list_products
from storefront.money import format_price


def test_checkout_returns_a_receipt() -> None:
    assert checkout({"pen": 2}) == "Order 1: 5.00"


def test_products_are_listed() -> None:
    assert list_products() == ["Ballpoint pen (2.50)", "Ink bottle (12.50)"]


def test_format_price() -> None:
    assert format_price(5) == "0.05"
