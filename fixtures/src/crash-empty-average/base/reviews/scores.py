"""Review scores."""


def average_rating(ratings: list[int]) -> float | None:
    """Return the mean of `ratings`, or None when there are none."""
    if not ratings:
        return None
    return sum(ratings) / len(ratings)
