"""Plain-text invoice report."""

from billing.invoice import calc


def render(items: list[tuple[str, int, int]]) -> str:
    """Return the report line for an invoice."""
    return f"Total: {calc(items) / 100:.2f}"
