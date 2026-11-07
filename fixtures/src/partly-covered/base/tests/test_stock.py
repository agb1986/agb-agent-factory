from inventory.stock import reserve


def test_reserve_takes_items_out_of_stock() -> None:
    assert reserve(10, 3) == 7
