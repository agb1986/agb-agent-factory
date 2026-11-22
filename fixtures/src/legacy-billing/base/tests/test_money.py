import pytest
from legacy_billing import money


@pytest.mark.parametrize(("text", "cents"), [("12.34", 1234), ("1,200.00", 120000), ("-0.50", -50)])
def test_parse_amount(text: str, cents: int) -> None:
    assert money.parse_amount(text) == cents


def test_format_cents() -> None:
    assert money.format_cents(-1205) == "-12.05"


def test_percent_rounds_half_up() -> None:
    assert money.percent_of(50, 5) == 3
