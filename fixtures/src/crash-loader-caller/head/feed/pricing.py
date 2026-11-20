"""Totals over the items of a parsed feed."""

from feed.loader import load_items


def line_total(item: dict[str, str]) -> int:
    """Return the item's price times its quantity, in cents."""
    return int(item["price"]) * int(item["quantity"])


def feed_total(text: str) -> int:
    """Return the value of the whole feed, in cents."""
    return sum(line_total(item) for item in load_items(text))
