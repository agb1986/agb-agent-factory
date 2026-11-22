"""Payments, put against the oldest unpaid invoices first."""

from legacy_billing import ledger, state, storage


def record_payment(customer_id: str, cents: int, reference: str) -> int:
    """Post a payment and return the part of it that no invoice absorbed."""
    ledger.post_entry(customer_id, "payment", reference, -cents)
    left = _allocate(customer_id, cents)
    if left:
        state.WARNINGS.append(f"{customer_id} overpaid by {left}")
    return left


def _allocate(customer_id: str, cents: int) -> int:
    left = cents
    for row in storage.read_rows("invoices"):
        if row["customer"] != customer_id or left == 0:
            continue
        open_cents = row["total"] + row["late_fee"] - row["paid"]
        part = min(open_cents, left)
        if part > 0:
            row["paid"] += part
            left -= part
            storage.replace_row("invoices", "number", row)
    return left
