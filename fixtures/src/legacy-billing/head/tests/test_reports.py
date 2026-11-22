from pathlib import Path

import pytest
from helpers import customer
from legacy_billing import billing, customers, reports


def _bill() -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    billing.run_billing("2024-03", "2024-03-31")


def test_reports(store: Path) -> None:
    _bill()
    assert reports.run_report("balances") == "ANN             12.30"
    assert reports.run_report("unpaid") == "INV-0001  2024-04-30 12.30"
    assert reports.run_report("warnings") == ""
    with pytest.raises(KeyError):
        reports.run_report("nothing")


def test_exports(store: Path) -> None:
    _bill()
    assert billing.export_ledger("csv").splitlines()[1] == "ann,invoice,INV-0001,12.30"
    assert billing.export_ledger("fixed") == "ann     INV-0001           12.30"
    with pytest.raises(billing.BillingError):
        billing.export_ledger("xml")


def test_statement(store: Path) -> None:
    _bill()
    assert billing.customer_statement("ann").splitlines() == [
        "Statement for ANN (EUR)",
        "invoice  INV-0001       12.30       12.30",
    ]
