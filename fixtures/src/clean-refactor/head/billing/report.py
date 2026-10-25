"""Plain-text invoice report."""

from billing.invoice import invoice_total


def render(items: list[tuple[str, int, int]]) -> str:
    """Return the report line for an invoice."""
    return f"Total: {invoice_total(items) / 100:.2f}"
