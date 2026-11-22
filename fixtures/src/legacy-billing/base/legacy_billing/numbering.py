"""Document numbers, counted in the shared state and saved with the other tables."""

from legacy_billing import state, storage


def _load() -> None:
    if not state.COUNTERS:
        for row in storage.read_rows("counters"):
            state.COUNTERS[row["kind"]] = row["last"]


def next_number(kind: str) -> str:
    """Return the next number of a kind of document, such as `INV-0001`."""
    _load()
    last = state.COUNTERS.get(kind, 0) + 1
    state.COUNTERS[kind] = last
    storage.replace_row("counters", "kind", {"kind": kind, "last": last})
    return f"{kind}-{last:04d}"
