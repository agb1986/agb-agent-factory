"""Plain text reports, looked up by name."""

from legacy_billing import customers, ledger, money, state, storage


def _pad(text: str, width: int) -> str:
    return text[:width].ljust(width)


def report_balances() -> str:
    """Return what every customer owes."""
    lines = []
    for customer in customers.all_customers():
        owed = money.format_cents(ledger.balance(customer["id"]))
        lines.append(f"{_pad(customer['name'], 16)}{owed}")
    return "\n".join(lines)


def report_unpaid() -> str:
    """Return the invoices that are not paid in full."""
    lines = []
    for row in storage.read_rows("invoices"):
        owed = row["total"] + row["late_fee"] - row["paid"]
        if owed > 0:
            lines.append(f"{_pad(row['number'], 10)}{row['due']} {money.format_cents(owed)}")
    return "\n".join(lines)


def report_warnings() -> str:
    """Return the warnings the current process has raised."""
    return "\n".join(state.WARNINGS)


REPORTS = {
    "balances": report_balances,
    "unpaid": report_unpaid,
    "warnings": report_warnings,
}


def run_report(name: str) -> str:
    """Return the report called `name`; an unknown name is a `KeyError`."""
    return REPORTS[name]()
