"""Price formatting."""


def format_price(cents: int) -> str:
    """Format cents as a decimal string, for example 1250 as '12.50'."""
    wholeUnits, remainder = divmod(cents, 100)
    return f"{wholeUnits}.{remainder:02d}"
