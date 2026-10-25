from billing.invoice import calc
from billing.report import render

ITEMS = [("pen", 2, 150), ("pad", 1, 400)]


def test_total_includes_tax() -> None:
    assert calc(ITEMS) == 840


def test_discount_is_taken_off_after_tax() -> None:
    assert calc(ITEMS, discount=40) == 800


def test_total_never_goes_below_zero() -> None:
    assert calc(ITEMS, discount=10_000) == 0


def test_report_line() -> None:
    assert render(ITEMS) == "Total: 8.40"
