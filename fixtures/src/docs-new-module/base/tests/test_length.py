import pytest
from units.length import metres_to_feet


def test_metres_to_feet() -> None:
    assert metres_to_feet(2) == pytest.approx(6.56168)
