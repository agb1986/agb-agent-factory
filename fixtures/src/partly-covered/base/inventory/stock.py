"""Stock levels."""


def reserve(on_hand: int, quantity: int) -> int:
    """Return the stock left after reserving `quantity` items out of `on_hand`."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if quantity > on_hand:
        raise ValueError("not enough stock")
    return on_hand - quantity
