"""Sends invoices by fax."""

from legacy_billing import settings, storage


def send_fax(number: str, text: str) -> None:
    """Queue one fax page per sixty lines of text."""
    lines = text.splitlines()
    for start in range(0, len(lines), 60):
        _queue_page(number, lines[start : start + 60])


def _queue_page(number: str, lines: list[str]) -> None:
    storage.append_row("fax", {"from": settings.FAX_LINE, "to": number, "lines": lines})
