from pathlib import Path

import pytest
from helpers import customer
from legacy_billing import audit, billing, customers, ledger, state, storage, usage


def test_run_bills_every_active_customer(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    customers.add_customer(customer("bob", active=False))
    assert billing.run_billing("2024-03", "2024-03-31") == 1
    row = storage.read_rows("invoices")[0]
    assert row["number"] == "INV-0001"
    assert row["total"] == 1230
    assert row["due"] == "2024-04-30"
    assert ledger.balance("ann") == 1230


def test_run_twice_warns_instead_of_billing_again(store: Path) -> None:
    customers.add_customer(customer("ann"))
    billing.run_billing("2024-03", "2024-03-31")
    assert billing.run_billing("2024-03", "2024-04-01") == 0
    assert state.WARNINGS == ["ann was already billed for 2024-03"]


def test_first_month_is_charged_in_part(store: Path) -> None:
    assert billing.prorate(3100, "2024-03-22", "2024-03") == 1000
    assert billing.prorate(3100, "2024-04-01", "2024-03") == 0


def test_usage_is_charged_by_meter(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    usage.record_usage("ann", "2024-03-02", "calls", 10)
    usage.record_usage("ann", "2024-03-09", "calls", 5)
    usage.record_usage("ann", "2024-04-01", "calls", 99)
    billing.run_billing("2024-03", "2024-03-31")
    lines = storage.read_rows("invoices")[0]["lines"]
    assert lines[1] == {"kind": "usage", "text": "15 x calls", "cents": 30}


def test_business_customer_abroad_pays_no_vat(store: Path) -> None:
    customers.add_customer(
        customer("ann", joined="2024-03-01", country="DE", vat_number="DE123456789")
    )
    customers.add_customer(customer("bob", joined="2024-03-01", country="DE"))
    billing.run_billing("2024-03", "2024-03-31")
    assert [row["vat"] for row in storage.read_rows("invoices")] == [0, 190]


def test_invoice_notice_is_queued(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    billing.run_billing("2024-03", "2024-03-31")
    assert storage.read_rows("outbox") == [
        {
            "to": "ann@example.test",
            "kind": "invoice",
            "text": "Invoice INV-0001 over 12.30 is due on 2024-04-30.",
        }
    ]


def test_audit_hook_sees_every_invoice(store: Path) -> None:
    audit.install()
    audit.install()
    customers.add_customer(customer("ann"))
    billing.run_billing("2024-03", "2024-03-31")
    assert storage.read_rows("audit") == [{"event": "posted", "number": "INV-0001"}]


def test_credit_note_lowers_the_balance(store: Path) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    billing.run_billing("2024-03", "2024-03-31")
    assert billing.issue_credit_note("INV-0001", 230) == "CRN-0001"
    assert ledger.balance("ann") == 1000
    with pytest.raises(billing.BillingError):
        billing.issue_credit_note("INV-0001", 5000)


def test_period_closes_once_everyone_is_billed(store: Path) -> None:
    customers.add_customer(customer("ann"))
    with pytest.raises(billing.PeriodError):
        billing.close_period("2024-03")
    billing.run_billing("2024-03", "2024-03-31")
    billing.close_period("2024-03")
    with pytest.raises(billing.PeriodError):
        billing.run_billing("2024-03", "2024-04-01")
