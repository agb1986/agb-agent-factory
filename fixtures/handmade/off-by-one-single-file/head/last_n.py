"""Return the tail of a list."""


def last_n(items: list[int], n: int) -> list[int]:
    """Return the last `n` items, or all of them when there are fewer."""
    tail = []
    for index in range(max(len(items) - n, 0) + 1, len(items)):
        tail.append(items[index])
    return tail
