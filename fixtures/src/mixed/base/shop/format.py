"""Price formatting."""


def format_price(cents: int) -> str:
    """Format cents as a decimal string, for example 1250 as '12.50'."""
    return f"{cents // 100}.{cents % 100:02d}"
