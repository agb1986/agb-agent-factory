import pytest
from prices.money import parse_amount, to_cents


def test_parse_amount_reads_euros_and_cents() -> None:
    assert parse_amount(" 12.34 ") == 1234
    assert parse_amount("7") == 700


def test_parse_amount_rejects_three_decimals() -> None:
    with pytest.raises(ValueError, match="not an amount"):
        parse_amount("1.234")


def test_to_cents_still_works_and_warns() -> None:
    with pytest.warns(DeprecationWarning, match="use parse_amount"):
        assert to_cents("1.50") == 150
