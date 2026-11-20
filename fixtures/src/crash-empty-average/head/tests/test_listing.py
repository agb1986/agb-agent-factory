from reviews.listing import product_line
from reviews.scores import average_rating


def test_average_of_two() -> None:
    assert average_rating([4, 5]) == 4.5


def test_line_shows_the_average() -> None:
    assert product_line("Mug", [3, 4, 5]) == "Mug: 4.0 stars"
