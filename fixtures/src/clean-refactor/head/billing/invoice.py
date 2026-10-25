"""Invoice totals."""

TAX_RATE = 0.2


def invoice_total(items: list[tuple[str, int, int]], discount: int = 0) -> int:
    """Return the invoice total in cents: the item lines plus tax, less a flat discount."""
    taxed = _with_tax(_subtotal(items))
    return max(taxed - discount, 0)


def _subtotal(items: list[tuple[str, int, int]]) -> int:
    """Return the sum of quantity times unit price over all lines, in cents."""
    subtotal = 0
    for _name, quantity, unit_price in items:
        subtotal += quantity * unit_price
    return subtotal


def _with_tax(amount: int) -> int:
    """Return `amount` plus tax, rounded to whole cents."""
    return amount + round(amount * TAX_RATE)
