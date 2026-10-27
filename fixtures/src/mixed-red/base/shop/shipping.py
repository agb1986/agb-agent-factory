"""Shipping charges."""

FREE_SHIPPING_THRESHOLD = 5000  # cents


def shipping_cost(subtotal: int) -> int:
    """Return the shipping charge in cents: free at or above the threshold, else a flat 499."""
    if subtotal >= FREE_SHIPPING_THRESHOLD:
        return 0
    return 499
