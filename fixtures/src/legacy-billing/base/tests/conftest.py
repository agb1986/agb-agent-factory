from collections.abc import Iterator
from pathlib import Path

import pytest
from legacy_billing import state, storage


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Path]:
    """Open an empty data directory, and forget the shared state before and after."""
    state.reset()
    storage.open_store(tmp_path)
    yield tmp_path
    state.reset()
