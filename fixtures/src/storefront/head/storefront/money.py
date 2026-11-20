"""Money is whole cents."""


def add(a: int, b: int) -> int:
    """Return the sum of two amounts in cents."""
    return a + b


def format_price(cents: int) -> str:
    """Return an amount in cents as a decimal string, such as 1250 -> "12.50"."""
    return f"{cents // 100}.{cents % 100:02d}"
