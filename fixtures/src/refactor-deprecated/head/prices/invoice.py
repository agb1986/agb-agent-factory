"""Total the lines of an invoice read from text."""

from prices.money import to_cents


def line_total(quantity: int, unit_price: str) -> int:
    """Return `quantity` times `unit_price`, an amount such as `2.50`, in cents."""
    if quantity < 0:
        raise ValueError(f"negative quantity: {quantity}")
    return quantity * to_cents(unit_price)


def invoice_total(lines: list[tuple[int, str]], shipping: str = "0") -> int:
    """Return the sum of `(quantity, unit_price)` lines plus `shipping`, in cents."""
    return sum(line_total(quantity, price) for quantity, price in lines) + to_cents(shipping)
