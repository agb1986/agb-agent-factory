"""Fixtures for the tool tests."""

from pathlib import Path

import pytest
from repo_helpers import Repo, build_repo


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A throwaway git repository with two commits and a symlink that points out of it."""
    return build_repo(tmp_path)
