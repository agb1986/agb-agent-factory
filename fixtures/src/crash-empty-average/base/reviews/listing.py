"""The product line the shop shows, with its average rating."""

from reviews.scores import average_rating


def product_line(name: str, ratings: list[int]) -> str:
    """Return `name` with its average rating, or "no ratings yet"."""
    average = average_rating(ratings)
    if average is None:
        return f"{name}: no ratings yet"
    return f"{name}: {average:.1f} stars"
