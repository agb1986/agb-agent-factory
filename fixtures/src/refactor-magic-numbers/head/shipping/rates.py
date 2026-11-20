"""Shipping costs for parcels."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Parcel:
    """A parcel to ship: its weight in grams and the order value it carries, in cents."""

    weight_grams: int
    order_cents: int


def shipping_cents(parcel: Parcel, express: bool = False) -> int:
    """Return what shipping `parcel` costs, in cents. Raises `ValueError` for an empty parcel."""
    if parcel.weight_grams <= 0:
        raise ValueError(f"a parcel weighs more than 0 grams, got {parcel.weight_grams}")
    if parcel.order_cents >= 5000 and not express:
        return 0
    cost = 499
    if parcel.weight_grams > 2000:
        cost += 150 * ((parcel.weight_grams - 2000 + 999) // 1000)
    if express:
        cost = cost * 3 // 2
    return cost
