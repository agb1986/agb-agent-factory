from pathlib import Path

from helpers import customer
from legacy_billing import billing, customers, ledger, payments, state, storage


def test_payment_goes_to_the_oldest_invoice_first(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    billing.run_billing("2024-03", "2024-03-31")
    billing.run_billing("2024-04", "2024-04-30")
    assert payments.record_payment("ann", 1500, "PAY-1") == 0
    assert [row["paid"] for row in storage.read_rows("invoices")] == [1230, 270]
    assert ledger.balance("ann") == 960


def test_overpayment_is_a_warning(store: Path) -> None:
    customers.add_customer(customer("ann"))
    assert payments.record_payment("ann", 100, "PAY-1") == 100
    assert state.WARNINGS == ["ann overpaid by 100"]
