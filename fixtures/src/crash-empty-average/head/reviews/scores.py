"""Review scores."""


def average_rating(ratings: list[int]) -> float:
    """Return the mean of `ratings`, rounded to one decimal place."""
    total = sum(ratings)
    return round(total / len(ratings), 1)
