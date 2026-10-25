"""Shared pytest fixtures, the live-key skip rules and the `--update-golden` option."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"

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
