"""Shared pytest fixtures and the live-key skip rules."""

import os

import pytest

# Marker name to the environment variable that holds the key its tests need.
_LIVE_KEYS = {"live": "ANTHROPIC_API_KEY", "live_jev": "TYPESAFE_API_KEY"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip each live test whose key variable is not set.

    `live` tests need `ANTHROPIC_API_KEY` and `live_jev` tests need `TYPESAFE_API_KEY`. The two
    are independent: having one key does not run the other's tests.
    """
    for marker, variable in _LIVE_KEYS.items():
        if os.environ.get(variable):
            continue
        skip = pytest.mark.skip(reason=f"{variable} not set")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)
