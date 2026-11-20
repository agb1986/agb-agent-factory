from shipping.rates import Parcel


def test_parcel_keeps_its_fields() -> None:
    parcel = Parcel(weight_grams=1200, order_cents=3000)
    assert (parcel.weight_grams, parcel.order_cents) == (1200, 3000)
