import pytest
from storefront.cart import Cart


def test_total_adds_up_lines() -> None:
    cart = Cart()
    cart.put("pen", 2)
    cart.put("ink")
    assert cart.total() == 1750


def test_unknown_sku_is_rejected() -> None:
    with pytest.raises(KeyError):
        Cart().put("nope")
