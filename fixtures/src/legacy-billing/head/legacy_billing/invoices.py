"""The invoice object that the billing run fills in."""

from typing import Any

from legacy_billing import money


class Invoice:
    """One invoice, while it is being put together."""

    def __init__(self, number: str, customer_id: str, period: str) -> None:
        self.number = number
        self.customer_id = customer_id
        self.period = period
        self.lines: list[dict[str, Any]] = []
        self.vat = 0

    def add_line(self, kind: str, text: str, cents: int) -> None:
        """Add one line; a negative amount is a discount."""
        self.lines.append({"kind": kind, "text": text, "cents": cents})

    def subtotal(self) -> int:
        """Return the sum of the lines, before VAT."""
        return sum(line["cents"] for line in self.lines)

    def charges(self) -> int:
        """Return the sum of the lines that are not discounts."""
        return sum(line["cents"] for line in self.lines if line["kind"] != "discount")

    @property
    def total(self) -> int:
        """The amount to pay, VAT included."""
        return self.subtotal() + self.vat

    def to_row(self, issued: str, due: str) -> dict[str, Any]:
        """Return the row that is saved for this invoice."""
        return {
            "number": self.number,
            "customer": self.customer_id,
            "period": self.period,
            "issued": issued,
            "due": due,
            "lines": self.lines,
            "vat": self.vat,
            "total": self.total,
            "paid": 0,
            "late_fee": 0,
        }

    def as_legacy_row(self) -> list[str]:
        """Return the columns the mainframe export used to read."""
        return [self.number, self.customer_id, money.format_cents(self.total)]

    def text(self) -> str:
        """Return the invoice as plain text."""
        rows = [f"Invoice {self.number} for {self.period}"]
        rows += [f"  {line['text']}: {money.format_cents(line['cents'])}" for line in self.lines]
        rows.append(f"  VAT: {money.format_cents(self.vat)}")
        rows.append(f"  Total: {money.format_cents(self.total)}")
        return "\n".join(rows)
