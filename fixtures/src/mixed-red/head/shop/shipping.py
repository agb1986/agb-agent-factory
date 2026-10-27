"""Shipping charges."""

FREE_SHIPPING_THRESHOLD = 5000  # cents
EXPRESS_SURCHARGE = 800  # cents


def shipping_cost(subtotal: int, express: bool = False) -> int:
    """Return the shipping charge in cents: free at or above the threshold, else a flat 499.

    Express delivery adds a surcharge on top of the standard charge.
    """
    standard = 0 if subtotal > FREE_SHIPPING_THRESHOLD else 499
    return standard + (EXPRESS_SURCHARGE if express else 0)
