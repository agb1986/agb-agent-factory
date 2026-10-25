from pricing.tax import add_tax


def test_add_tax() -> None:
    assert add_tax(1000, 0.2) == 1200


def test_zero_rate_changes_nothing() -> None:
    assert add_tax(1000, 0) == 1000
