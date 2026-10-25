"""Discount arithmetic."""


def apply_discount(cents: int, percent: int) -> int:
    """Return `cents` reduced by `percent` per cent, rounded to whole cents."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return cents - round(cents * percent / 100)
