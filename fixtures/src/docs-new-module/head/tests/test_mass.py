from collections.abc import Callable

import pytest
from units.mass import kg_to_lb, lb_to_kg


def test_kg_to_lb_and_back() -> None:
    assert kg_to_lb(10) == pytest.approx(22.0462)
    assert lb_to_kg(kg_to_lb(3)) == pytest.approx(3)


@pytest.mark.parametrize("convert", [kg_to_lb, lb_to_kg])
def test_a_negative_mass_is_refused(convert: Callable[[float], float]) -> None:
    with pytest.raises(ValueError, match="negative"):
        convert(-1)
