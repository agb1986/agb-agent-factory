"""Shipping costs for parcels."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Parcel:
    """A parcel to ship: its weight in grams and the order value it carries, in cents."""

    weight_grams: int
    order_cents: int
