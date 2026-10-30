"""Shared pytest fixtures, the live opt-in and key rules and the `--update-golden` option."""

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"

# Marker name to the environment variable that holds the key its tests need.
_LIVE_KEYS = {"live": "ANTHROPIC_API_KEY", "live_jev": "TYPESAFE_API_KEY"}

# Live tests spend real money, so a key in the environment is not enough: this must be "1" too.
LIVE_OPT_IN = "FACTORY_LIVE"


def live_selection(marker: str, env: Mapping[str, str]) -> str | None:
    """Return what the opt-in rule does with a test carrying a live `marker`.

    `"deselect"` when `FACTORY_LIVE` is not `1`, so a plain `pytest` selects no live test even
    with both keys set. `"skip"` when opted in but the marker's key variable is unset or empty.
    `None` when the test runs.
    """
    if env.get(LIVE_OPT_IN) != "1":
        return "deselect"
    if not env.get(_LIVE_KEYS[marker]):
        return "skip"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply the live opt-in rule: deselect without `FACTORY_LIVE=1`, then skip without the key.

    `live` tests need `ANTHROPIC_API_KEY` and `live_jev` tests need `TYPESAFE_API_KEY`. The two
    are independent: having one key does not run the other's tests.
    """
    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        action = None
        for marker, variable in _LIVE_KEYS.items():
            if item.get_closest_marker(marker) is None:
                continue
            action = live_selection(marker, os.environ)
            if action == "skip":
                item.add_marker(pytest.mark.skip(reason=f"{variable} not set"))
            if action:
                break
        (deselected if action == "deselect" else kept).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add `--update-golden`, which rewrites the files under `tests/golden/` instead of checking."""
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/*.json from the current output instead of comparing to it",
    )


@pytest.fixture
def golden(request: pytest.FixtureRequest) -> Callable[[str, Any], None]:
    """Return `check(name, document)`, which compares `document` to `tests/golden/<name>.json`.

    With `--update-golden` it writes the file instead, so a deliberate output change is one command
    and a reviewable diff. A missing file fails with the same hint.
    """
    update = bool(request.config.getoption("--update-golden"))

    def check(name: str, document: Any) -> None:
        path = GOLDEN_DIR / f"{name}.json"
        if update:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            return
        assert path.exists(), f"{path} is missing: run `pytest --update-golden` to create it"
        expected = json.loads(path.read_text(encoding="utf-8"))
        assert document == expected, (
            f"{name} output changed: run `pytest --update-golden` if intended"
        )

    return check
