"""A live read of github.com through the two read-only GitHub tools.

Needs `FACTORY_LIVE=1` and a `GH_TOKEN` that can read public repositories. It reads issue #84 and
pull request #1 of this project's own repository through a throwaway clone whose `origin` points
at it, and writes nothing: the outside writes are covered against a fake `gh` in
tests/tools/test_github.py.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tools import github

ORIGIN = "https://github.com/agb1986/agb-agent-factory.git"


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", ORIGIN], check=True)
    return tmp_path


@pytest.mark.live_github
def test_read_issue_and_read_pr(clone: Path) -> None:
    assert github.origin_repository(clone).full_name == "agb1986/agb-agent-factory"
    issue = json.loads(github.read_issue(clone, 84))
    assert issue["number"] == 84
    assert issue["is_pull_request"] is False
    assert issue["note"] == github.UNTRUSTED_NOTE
    pull = json.loads(github.read_pr(clone, 1))
    assert pull["number"] == 1
    assert pull["base"]["repository"] == "agb1986/agb-agent-factory"
    assert pull["files"]
