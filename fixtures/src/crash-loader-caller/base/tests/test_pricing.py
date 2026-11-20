from feed.loader import load_items
from feed.pricing import feed_total


def test_loader_skips_blank_lines() -> None:
    assert [item["sku"] for item in load_items("A1,100,2\n\nB2,250,1\n")] == ["A1", "B2"]


def test_feed_total_adds_every_line() -> None:
    assert feed_total("A1,100,2\nB2,250,1\n") == 450
