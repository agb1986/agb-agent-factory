import pytest
from prices.invoice import invoice_total, line_total


def test_line_total_multiplies_the_unit_price() -> None:
    assert line_total(3, "2.50") == 750


def test_line_total_rejects_a_negative_quantity() -> None:
    with pytest.raises(ValueError, match="negative quantity: -1"):
        line_total(-1, "1.00")


def test_invoice_total_adds_shipping() -> None:
    assert invoice_total([(2, "1.25"), (1, "0.99")], shipping="4.90") == 839


def test_invoice_total_rejects_a_malformed_price() -> None:
    with pytest.raises(ValueError, match="not an amount"):
        invoice_total([(1, "1,25")])
