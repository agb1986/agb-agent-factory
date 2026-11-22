"""Metered usage, one row per reading."""

from legacy_billing import storage


def record_usage(customer_id: str, day: str, meter: str, quantity: int) -> None:
    """Save one reading."""
    storage.append_row(
        "usage", {"customer": customer_id, "day": day, "meter": meter, "quantity": quantity}
    )


def usage_for(customer_id: str, period: str) -> dict[str, int]:
    """Return the quantity per meter that a customer used in a period."""
    totals: dict[str, int] = {}
    for row in storage.read_rows("usage"):
        if row["customer"] == customer_id and row["day"].startswith(period):
            totals[row["meter"]] = totals.get(row["meter"], 0) + row["quantity"]
    return totals
