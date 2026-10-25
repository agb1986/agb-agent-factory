"""Return the tail of a list."""


def last_n(items: list[int], n: int) -> list[int]:
    """Return the last `n` items, or all of them when there are fewer."""
    return items[max(len(items) - n, 0) :]
