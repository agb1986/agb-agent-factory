"""Lengths."""

FEET_PER_METRE = 3.28084
"""How many feet make one metre."""


def metres_to_feet(metres: float) -> float:
    """Return `metres` in feet."""
    return metres * FEET_PER_METRE
