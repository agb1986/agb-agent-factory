"""The audit trail. It hooks itself into the billing run instead of being called by it."""

from typing import Any

from legacy_billing import state, storage


def install() -> None:
    """Make the billing run report every posted invoice here."""
    if log_posting not in state.HOOKS:
        state.HOOKS.append(log_posting)


def log_posting(row: dict[str, Any]) -> None:
    """Save the trail of one posted invoice."""
    storage.append_row("audit", {"event": "posted", "number": row["number"]})
