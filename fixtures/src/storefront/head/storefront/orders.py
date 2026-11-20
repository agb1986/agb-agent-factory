"""Placing orders."""

from dataclasses import dataclass

from storefront.cart import Cart
from storefront.money import format_price


@dataclass(frozen=True)
class Order:
    """A placed order: its number, its lines and its total in cents."""

    number: int
    lines: dict[str, int]
    total: int


_PLACED: list[Order] = []


def place(cart: Cart) -> Order:
    """Turn a cart into an order and remember it."""
    order = Order(len(_PLACED) + 1, dict(cart.lines), cart.total())
    _PLACED.append(order)
    return order


def receipt(order: Order) -> str:
    """Return a one-line receipt for an order."""
    return f"Order {order.number}: {format_price(order.total)}"
