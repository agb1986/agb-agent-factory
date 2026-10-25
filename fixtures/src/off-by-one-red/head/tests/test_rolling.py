import pytest
from series.rolling import rolling_sums


def test_single_window() -> None:
    assert rolling_sums([1, 2, 3], 3) == [6]


def test_first_window_is_the_oldest() -> None:
    assert rolling_sums([1, 2, 3, 4], 2)[0] == 3


def test_window_larger_than_series_is_empty() -> None:
    assert rolling_sums([1, 2], 5) == []


def test_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError):
        rolling_sums([1, 2, 3], 0)


def test_last_window_is_included() -> None:
    assert rolling_sums([1, 2, 3, 4], 2) == [3, 5, 7]
