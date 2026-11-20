import pytest
from report.lines import invoice_line


def test_invoice_line_pads_cents() -> None:
    assert invoice_line("tea", 305) == "tea: 3.05 EUR"


def test_invoice_line_rejects_a_negative_amount() -> None:
    with pytest.raises(ValueError, match="negative amount for tea: -1"):
        invoice_line("tea", -1)
