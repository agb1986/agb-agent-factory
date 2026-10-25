"""Invoice totals."""

TAX_RATE = 0.2


def calc(items: list[tuple[str, int, int]], discount: int = 0) -> int:
    """Return the invoice total in cents: the item lines plus tax, less a flat discount."""
    t = 0
    for _name, qty, unit in items:
        t += qty * unit
    t = t + round(t * TAX_RATE)
    return max(t - discount, 0)
