"""Parse the supplier feed: one `sku,price_cents,quantity` line per item."""


def load_items(text: str) -> list[dict[str, str]]:
    """Return one mapping per item line, skipping blank lines and lines without a price."""
    items = []
    for line in text.splitlines():
        if not line.strip():
            continue
        sku, price, quantity = (part.strip() for part in line.split(","))
        if not price:
            continue
        items.append({"sku": sku, "price": price, "quantity": quantity})
    return items
