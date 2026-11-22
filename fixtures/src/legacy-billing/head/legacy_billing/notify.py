"""Letters to customers, queued in the outbox table."""

from legacy_billing import customers, money, storage


def queue_notice(customer_id: str, kind: str, text: str) -> None:
    """Put one letter into the outbox."""
    customer = customers.get_customer(customer_id)
    storage.append_row("outbox", {"to": customer["email"], "kind": kind, "text": text})


def late_fee_text(number: str, cents: int, days: int) -> str:
    """Return the text of a late fee letter."""
    return f"Invoice {number} is {days} days overdue; a fee of {money.format_cents(cents)} applies."


def invoice_text(number: str, cents: int, due: str) -> str:
    """Return the text of the letter that goes with an invoice."""
    return f"Invoice {number} over {money.format_cents(cents)} is due on {due}."
