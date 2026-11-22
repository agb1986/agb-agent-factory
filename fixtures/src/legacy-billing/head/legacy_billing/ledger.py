"""The ledger: one row per posting, and balances over them."""

from typing import Any

from legacy_billing import state, storage


def post_entry(customer_id: str, kind: str, reference: str, cents: int) -> dict[str, Any]:
    """Save one posting. A charge is positive, a payment or credit negative."""
    entry = {
        "customer": customer_id,
        "kind": kind,
        "reference": reference,
        "cents": cents,
        "period": state.RUN.get("period", ""),
    }
    storage.append_row("ledger", entry)
    return entry


def entries(customer_id: str | None = None) -> list[dict[str, Any]]:
    """Return the postings of a customer, or all of them."""
    rows = storage.read_rows("ledger")
    return [row for row in rows if customer_id in (None, row["customer"])]


def balance(customer_id: str) -> int:
    """Return what a customer owes in cents."""
    return sum(row["cents"] for row in entries(customer_id))
