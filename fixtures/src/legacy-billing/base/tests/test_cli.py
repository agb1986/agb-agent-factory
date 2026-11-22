from pathlib import Path

import pytest
from helpers import customer
from legacy_billing import customers
from legacy_billing.cli import main


def _run(store: Path, *argv: str) -> int:
    return main(["--data", str(store), *argv])


def test_bill_then_report(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    assert _run(store, "bill", "2024-03", "--issued", "2024-03-31") == 0
    assert _run(store, "report", "balances") == 0
    assert capsys.readouterr().out == "1 invoices\nANN             12.30\n"


def test_add_customer(store: Path) -> None:
    argv = ["add-customer", "ann", "--name", "Ann", "--email", "a@example.test"]
    assert _run(store, *argv, "--joined", "2024-03-01") == 0
    assert customers.get_customer("ann")["plan"] == "basic"


def test_import_usage(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    readings = store / "readings.csv"
    readings.write_text(
        "customer,day,meter,quantity\nann,2024-03-02,calls,3\nann,2024-03-03,water,1\n",
        encoding="utf-8",
    )
    assert _run(store, "import-usage", str(readings)) == 0
    assert _run(store, "report", "warnings") == 0
    assert capsys.readouterr().out == "1 readings\nunknown meter water\n"


def test_late_fees_pay_and_credit(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    customers.add_customer(customer("ann", joined="2024-03-01"))
    _run(store, "bill", "2024-03", "--issued", "2024-03-31")
    assert _run(store, "late-fees", "2024-06-01") == 0
    assert _run(store, "pay", "ann", "1800", "PAY-1") == 0
    assert _run(store, "credit", "INV-0001", "100") == 0
    out = capsys.readouterr().out
    assert out == "1 invoices\n1 late fees\n70 left over\nCRN-0001\n"


def test_close_reports_a_missing_invoice(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    customers.add_customer(customer("ann"))
    assert _run(store, "close", "2024-03") == 1
    assert capsys.readouterr().err == "ann has no invoice for 2024-03\n"
