"""The functions the command line and a web layer call."""

from storefront.cart import Cart
from storefront.catalog import PRODUCTS, label
from storefront.orders import place, receipt


def list_products() -> list[str]:
    """Return one display line per product."""
    return [label(product) for product in PRODUCTS.values()]


def checkout(items: dict[str, int]) -> str:
    """Put `items` in a cart, place the order and return its receipt."""
    cart = Cart()
    for sku, quantity in items.items():
        cart.put(sku, quantity)
    return receipt(place(cart))
