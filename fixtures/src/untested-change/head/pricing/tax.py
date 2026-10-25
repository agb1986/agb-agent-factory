"""Tax arithmetic."""


def add_tax(cents: int, rate: float) -> int:
    """Return `cents` plus tax at `rate`, rounded to whole cents."""
    return cents + round(cents * rate)
