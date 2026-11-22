"""Customer rows, cached in the shared state the first time they are read."""

from typing import Any

from legacy_billing import dates, state, storage


def add_customer(row: dict[str, Any]) -> None:
    """Save a customer and forget the cached rows."""
    storage.replace_row("customers", "id", row)
    state.CACHE.pop("customers", None)


def all_customers() -> list[dict[str, Any]]:
    """Return every customer, ordered by id."""
    if "customers" not in state.CACHE:
        state.CACHE["customers"] = {row["id"]: row for row in storage.read_rows("customers")}
    cached = state.CACHE["customers"]
    return [cached[key] for key in sorted(cached)]


def get_customer(customer_id: str) -> dict[str, Any]:
    """Return one customer; an unknown id is a `KeyError`."""
    for row in all_customers():
        if row["id"] == customer_id:
            return row
    raise KeyError(customer_id)


def months_active(customer: dict[str, Any], period: str) -> int:
    """Return how many whole months the customer has been with us at the start of `period`."""
    joined = dates.parse_day(customer["joined"])
    start = dates.period_start(period)
    return (start.year - joined.year) * 12 + start.month - joined.month


class CustomerMerger:
    """Merges two customer rows that turned out to be the same customer."""

    def __init__(self, keep: dict[str, Any], drop: dict[str, Any]) -> None:
        self.keep = keep
        self.drop = drop

    def merge(self) -> dict[str, Any]:
        """Return the row to keep, with the gaps filled from the other."""
        merged = dict(self.keep)
        for field in self._gaps():
            merged[field] = self.drop[field]
        return merged

    def _gaps(self) -> list[str]:
        return [field for field in self.drop if not self.keep.get(field)]
