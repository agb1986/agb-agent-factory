"""A shopping cart."""

from storefront.catalog import find
from storefront.money import add


class Cart:
    """Quantities of products by SKU."""

    def __init__(self) -> None:
        self.lines: dict[str, int] = {}

    def put(self, sku: str, quantity: int = 1) -> None:
        """Add `quantity` of a product; an unknown SKU raises KeyError."""
        find(sku)
        self.lines[sku] = self.lines.get(sku, 0) + quantity

    def total(self) -> int:
        """Return the cart total in cents."""
        total = 0
        for sku, quantity in self.lines.items():
            total = add(total, find(sku).price * quantity)
        return total
