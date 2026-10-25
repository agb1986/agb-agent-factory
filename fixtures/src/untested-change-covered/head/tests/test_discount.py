import pytest
from pricing.discount import apply_discount


def test_takes_a_percentage_off() -> None:
    assert apply_discount(1000, 25) == 750


def test_rounds_to_whole_cents() -> None:
    assert apply_discount(333, 50) == 333 - round(166.5)


def test_rejects_a_percentage_over_100() -> None:
    with pytest.raises(ValueError):
        apply_discount(1000, 101)


def test_rejects_a_negative_percentage() -> None:
    with pytest.raises(ValueError):
        apply_discount(1000, -1)
