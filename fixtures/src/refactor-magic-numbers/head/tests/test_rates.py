import pytest
from shipping.rates import Parcel, shipping_cents


def test_parcel_keeps_its_fields() -> None:
    parcel = Parcel(weight_grams=1200, order_cents=3000)
    assert (parcel.weight_grams, parcel.order_cents) == (1200, 3000)


def test_light_parcel_pays_the_base_rate() -> None:
    assert shipping_cents(Parcel(2000, 4999)) == 499


def test_orders_from_fifty_euros_ship_free() -> None:
    assert shipping_cents(Parcel(9000, 5000)) == 0


def test_each_started_kilogram_over_two_costs_extra() -> None:
    assert shipping_cents(Parcel(2001, 100)) == 649
    assert shipping_cents(Parcel(4000, 100)) == 799


def test_express_costs_half_again_and_is_never_free() -> None:
    assert shipping_cents(Parcel(1000, 9000), express=True) == 748
    assert shipping_cents(Parcel(3500, 100), express=True) == 1198


def test_empty_parcel_is_refused() -> None:
    with pytest.raises(ValueError, match="more than 0 grams, got 0"):
        shipping_cents(Parcel(0, 100))
