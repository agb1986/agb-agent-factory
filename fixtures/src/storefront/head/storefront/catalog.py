"""The product catalog."""

from dataclasses import dataclass

from storefront.money import format_price


@dataclass(frozen=True)
class Product:
    """One product for sale; the price is in cents."""

    sku: str
    name: str
    price: int


PRODUCTS = {
    "pen": Product("pen", "Ballpoint pen", 250),
    "ink": Product("ink", "Ink bottle", 1250),
}


def find(sku: str) -> Product:
    """Return the product with `sku`, or raise KeyError."""
    return PRODUCTS[sku]


def label(product: Product) -> str:
    """Return the name and price of a product for display."""
    return f"{product.name} ({format_price(product.price)})"
