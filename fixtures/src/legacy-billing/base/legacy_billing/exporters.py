"""Ledger exports. The billing module picks one by the name of the format."""

from typing import Any

from legacy_billing import money


def _pad(text: str, width: int) -> str:
    return text[:width].ljust(width)


def export_csv(rows: list[dict[str, Any]]) -> str:
    """Return the postings as comma-separated values."""
    lines = ["customer,kind,reference,amount"]
    for row in rows:
        amount = money.format_cents(row["cents"])
        lines.append(f"{row['customer']},{row['kind']},{row['reference']},{amount}")
    return "\n".join(lines)


def export_fixed(rows: list[dict[str, Any]]) -> str:
    """Return the postings in fixed-width columns."""
    lines = []
    for row in rows:
        amount = money.format_cents(row["cents"]).rjust(12)
        lines.append(f"{_pad(row['customer'], 8)}{_pad(row['reference'], 12)}{amount}")
    return "\n".join(lines)
