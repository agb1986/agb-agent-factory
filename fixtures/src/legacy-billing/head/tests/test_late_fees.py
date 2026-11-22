from pathlib import Path

from helpers import customer
from legacy_billing import billing, customers, ledger, payments, state, storage


def _bill(plan: str = "basic") -> None:
    customers.add_customer(customer("ann", joined="2024-03-01", plan=plan))
    billing.run_billing("2024-03", "2024-03-31")


def test_no_fee_within_the_grace_days(store: Path) -> None:
    _bill()
    assert billing.apply_late_fees("2024-05-07") == 0
    assert ledger.balance("ann") == 1230


def test_small_invoice_gets_the_flat_fee(store: Path) -> None:
    _bill()
    assert billing.apply_late_fees("2024-05-08") == 1
    assert storage.read_rows("invoices")[0]["late_fee"] == 500
    assert ledger.balance("ann") == 1730


def test_large_invoice_gets_a_share_of_what_is_open(store: Path) -> None:
    _bill("site")
    payments.record_payment("ann", 600, "PAY-1")
    billing.apply_late_fees("2024-06-01")
    assert storage.read_rows("invoices")[0]["late_fee"] == 500
    assert billing.late_fee_for({"total": 60000, "paid": 10000}) == 1000


def test_fee_is_charged_once_and_counted(store: Path) -> None:
    _bill()
    billing.apply_late_fees("2024-06-01")
    assert billing.apply_late_fees("2024-07-01") == 0
    assert state.RUN["late_fees"] == 1


def test_paid_invoice_gets_no_fee(store: Path) -> None:
    _bill()
    payments.record_payment("ann", 1230, "PAY-1")
    assert billing.overdue_invoices("2024-06-01") == []


def test_customer_is_told(store: Path) -> None:
    _bill()
    billing.apply_late_fees("2024-05-10")
    notice = storage.read_rows("outbox")[-1]
    assert notice["kind"] == "late_fee"
    assert notice["text"] == "Invoice INV-0001 is 10 days overdue; a fee of 5.00 applies."
