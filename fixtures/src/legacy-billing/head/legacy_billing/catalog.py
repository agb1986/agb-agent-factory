"""Plans, meter prices and promotion codes."""

from typing import Any

PLANS: dict[str, int] = {"basic": 1000, "team": 4500, "site": 20000}
METERS: dict[str, int] = {"calls": 2, "storage": 15, "seats": 300}

PROMOS: dict[str, dict[str, Any]] = {
    "WELCOME10": {"percent": 10, "limit": 1},
    "PARTNER20": {"percent": 20, "limit": 6},
}


def plan_price(plan: str) -> int:
    """Return the monthly price of a plan in cents."""
    return PLANS[plan]


def unit_price(meter: str) -> int:
    """Return the price of one unit of a meter in cents."""
    return METERS[meter]


def find_promo(code: str) -> dict[str, Any] | None:
    """Return the promotion of a code, or None when the code is unknown."""
    return PROMOS.get(code.upper())
