from collections.abc import Callable

import pytest
from report.lines import credit_line, invoice_line, refund_line


def test_invoice_line_pads_cents() -> None:
    assert invoice_line("tea", 305) == "tea: 3.05 EUR"


def test_invoice_line_rejects_a_negative_amount() -> None:
    with pytest.raises(ValueError, match="negative amount for tea: -1"):
        invoice_line("tea", -1)


def test_refund_line_has_a_minus_sign() -> None:
    assert refund_line("tea", 1200) == "tea: -12.00 EUR"


def test_credit_line_has_a_plus_sign() -> None:
    assert credit_line("tea", 7) == "tea: +0.07 EUR"


@pytest.mark.parametrize("line", [refund_line, credit_line])
def test_refund_and_credit_reject_a_negative_amount(line: Callable[[str, int], str]) -> None:
    with pytest.raises(ValueError, match="negative amount for tea: -5"):
        line("tea", -5)
