"""Tests for the work-tree read tools `fs.list_files` and `fs.read_file`."""

import pytest
from repo_helpers import Repo

from tools import PathEscapeError, ToolNotFoundError
from tools.fs import list_files, read_file


def test_list_files_includes_tracked_and_new_files(repo: Repo) -> None:
    (repo.root / "new.py").write_text("x = 1\n")
    listed = list_files(repo.root, max_bytes=65_536).splitlines()
    assert {"README.md", "src/app.py", "new.py"} <= set(listed)


def test_list_files_leaves_out_ignored_files(repo: Repo) -> None:
    (repo.root / ".gitignore").write_text("*.log\n")
    (repo.root / "run.log").write_text("noise\n")
    assert "run.log" not in list_files(repo.root, max_bytes=65_536).splitlines()


def test_list_files_is_truncated(repo: Repo) -> None:
    assert len(list_files(repo.root, max_bytes=10).encode()) < 200


def test_read_file_sees_uncommitted_edits(repo: Repo) -> None:
    (repo.root / "src" / "app.py").write_text("changed\n")
    assert read_file(repo.root, "src/app.py", max_bytes=65_536) == "changed\n"


@pytest.mark.parametrize("path", ["../outside/secret.txt", "leak.txt", "linkdir/secret.txt"])
def test_read_file_refuses_paths_that_leave_the_root(repo: Repo, path: str) -> None:
    with pytest.raises(PathEscapeError):
        read_file(repo.root, path, max_bytes=65_536)


@pytest.mark.parametrize("path", ["missing.py", "src"])
def test_read_file_needs_a_file(repo: Repo, path: str) -> None:
    with pytest.raises(ToolNotFoundError):
        read_file(repo.root, path, max_bytes=65_536)
