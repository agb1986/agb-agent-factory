"""The live opt-in rule in `tests/conftest.py`: `FACTORY_LIVE=1` plus the key, else no live test."""

import pytest

from tests.conftest import live_selection

KEYS = {"live": "ANTHROPIC_API_KEY", "live_jev": "TYPESAFE_API_KEY"}


@pytest.mark.parametrize("marker", KEYS)
def test_keys_without_opt_in_deselect(marker: str) -> None:
    env = {"ANTHROPIC_API_KEY": "x", "TYPESAFE_API_KEY": "x"}
    assert live_selection(marker, env) == "deselect"


@pytest.mark.parametrize("value", ["", "0", "true"])
def test_only_one_opts_in(value: str) -> None:
    env = {"FACTORY_LIVE": value, "ANTHROPIC_API_KEY": "x"}
    assert live_selection("live", env) == "deselect"


@pytest.mark.parametrize("marker", KEYS)
def test_opt_in_without_key_skips(marker: str) -> None:
    assert live_selection(marker, {"FACTORY_LIVE": "1"}) == "skip"


def test_opt_in_with_other_markers_key_skips() -> None:
    env = {"FACTORY_LIVE": "1", "ANTHROPIC_API_KEY": "x"}
    assert live_selection("live_jev", env) == "skip"
    assert live_selection("live", env) is None


def test_opt_in_with_key_runs() -> None:
    env = {"FACTORY_LIVE": "1", "ANTHROPIC_API_KEY": "x", "TYPESAFE_API_KEY": "x"}
    assert live_selection("live", env) is None
    assert live_selection("live_jev", env) is None
