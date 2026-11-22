"""Amounts are integers in cents; these helpers convert and round them."""

from decimal import ROUND_HALF_UP, Decimal


def percent_of(cents: int, percent: int | Decimal) -> int:
    """Return `percent` per cent of `cents`, rounded half up."""
    share = Decimal(cents) * Decimal(percent) / Decimal(100)
    return int(share.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_cents(cents: int) -> str:
    """Return `cents` as `12.34`, with a leading minus when negative."""
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def parse_amount(text: str) -> int:
    """Return the cents in text such as `12.34` or `1,200.00`."""
    return int(Decimal(text.replace(",", "")) * 100)


def to_major_units(cents: int) -> Decimal:
    """Return `cents` as a decimal amount in the major unit of the currency."""
    return Decimal(cents) / Decimal(100)
