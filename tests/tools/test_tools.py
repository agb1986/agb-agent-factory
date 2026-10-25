"""Tests for the git and rg tools against a temporary repository."""

import shutil
from pathlib import Path

import pytest
from repo_helpers import SECRET, Repo

from tools import (
    PathEscapeError,
    ToolInputError,
    ToolNotFoundError,
    ToolUnavailableError,
)
from tools import git as git_tools
from tools import rg as rg_tools
from tools._common import truncate

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not on PATH")


class TestReadFile:
    def test_reads_committed_content_at_a_ref(self, repo: Repo) -> None:
        assert git_tools.read_file(repo.root, "src/app.py", repo.first) == (
            "def greet():\n    return 'hello'\n"
        )
        assert "hello, world" in git_tools.read_file(repo.root, "src/app.py", "HEAD")

    def test_reads_the_committed_blob_not_the_working_tree(self, repo: Repo) -> None:
        (repo.root / "README.md").write_text("changed on disk\n")
        assert git_tools.read_file(repo.root, "README.md", "HEAD") == "# demo\n"

    def test_accepts_dot_dot_that_stays_inside(self, repo: Repo) -> None:
        assert git_tools.read_file(repo.root, "src/../README.md", "HEAD") == "# demo\n"

    def test_accepts_absolute_path_inside_the_root(self, repo: Repo) -> None:
        assert git_tools.read_file(repo.root, str(repo.root / "README.md"), "HEAD") == "# demo\n"

    @pytest.mark.parametrize(
        "path", ["../outside/secret.txt", "src/../../outside/secret.txt", "../repo/../outside"]
    )
    def test_rejects_dot_dot(self, repo: Repo, path: str) -> None:
        with pytest.raises(PathEscapeError, match="outside") as err:
            git_tools.read_file(repo.root, path, "HEAD")
        assert path in err.value.message

    def test_rejects_absolute_path_outside(self, repo: Repo) -> None:
        target = str(repo.outside / "secret.txt")
        with pytest.raises(PathEscapeError) as err:
            git_tools.read_file(repo.root, target, "HEAD")
        assert target in err.value.message

    def test_rejects_symlink_to_a_file_outside(self, repo: Repo) -> None:
        with pytest.raises(PathEscapeError) as err:
            git_tools.read_file(repo.root, "leak.txt", "HEAD")
        assert "leak.txt" in err.value.message

    def test_rejects_path_through_a_symlinked_directory(self, repo: Repo) -> None:
        with pytest.raises(PathEscapeError, match="linkdir/secret.txt"):
            git_tools.read_file(repo.root, "linkdir/secret.txt", "HEAD")

    def test_unknown_ref_and_missing_file_are_not_found(self, repo: Repo) -> None:
        with pytest.raises(ToolNotFoundError, match="no-such-ref"):
            git_tools.read_file(repo.root, "README.md", "no-such-ref")
        with pytest.raises(ToolNotFoundError, match="nope.txt"):
            git_tools.read_file(repo.root, "nope.txt", "HEAD")
        with pytest.raises(ToolNotFoundError, match="src"):
            git_tools.read_file(repo.root, "src", "HEAD")  # a directory is not a file

    def test_repository_root_is_not_a_file(self, repo: Repo) -> None:
        with pytest.raises(ToolInputError):
            git_tools.read_file(repo.root, ".", "HEAD")

    @pytest.mark.parametrize("ref", ["--output=pwned", "-p", "", "HEAD\nmain"])
    def test_rejects_refs_git_would_read_as_options(self, repo: Repo, ref: str) -> None:
        with pytest.raises(ToolInputError):
            git_tools.read_file(repo.root, "README.md", ref)
        assert not (repo.root / "pwned").exists()

    def test_truncates_with_a_marker(self, repo: Repo) -> None:
        out = git_tools.read_file(repo.root, "src/app.py", "HEAD", max_bytes=10)
        lines = out.splitlines()
        full = len(git_tools.read_file(repo.root, "src/app.py", "HEAD").encode())
        assert lines[0] == "def greet("
        assert lines[-1] == f"[truncated: {full - 10} bytes dropped, limit 10 bytes]"
        assert sum("[truncated" in line for line in lines) == 1


class TestShowDiff:
    def test_diff_between_commits(self, repo: Repo) -> None:
        out = git_tools.show_diff(repo.root, repo.first, repo.second)
        assert "-    return 'hello'" in out
        assert "+    return 'hello, world'" in out

    def test_diff_limited_to_a_path(self, repo: Repo) -> None:
        out = git_tools.show_diff(repo.root, repo.first, "HEAD", "src/app.py")
        assert "src/app.py" in out and "leak.txt" not in out
        assert git_tools.show_diff(repo.root, repo.first, "HEAD", "README.md") == ""

    def test_empty_diff_is_empty(self, repo: Repo) -> None:
        assert git_tools.show_diff(repo.root, "HEAD", "HEAD") == ""

    def test_ignores_configured_external_diff(self, repo: Repo) -> None:
        marker = repo.root.parent / "ran"
        repo.git("config", "diff.external", f"touch {marker}")
        git_tools.show_diff(repo.root, repo.first, repo.second)
        assert not marker.exists()

    @pytest.mark.parametrize("path", ["../outside", "leak.txt", "linkdir/secret.txt"])
    def test_rejects_paths_that_escape(self, repo: Repo, path: str) -> None:
        with pytest.raises(PathEscapeError):
            git_tools.show_diff(repo.root, repo.first, "HEAD", path)

    def test_rejects_absolute_path_outside(self, repo: Repo) -> None:
        with pytest.raises(PathEscapeError, match="secret.txt"):
            git_tools.show_diff(repo.root, repo.first, "HEAD", str(repo.outside / "secret.txt"))

    def test_unknown_ref_is_not_found(self, repo: Repo) -> None:
        with pytest.raises(ToolNotFoundError, match="nope"):
            git_tools.show_diff(repo.root, "nope", "HEAD")

    def test_rejects_option_like_refs(self, repo: Repo) -> None:
        with pytest.raises(ToolInputError):
            git_tools.show_diff(repo.root, "--output=pwned", "HEAD")
        assert not (repo.root / "pwned").exists()

    def test_truncates_with_a_marker(self, repo: Repo) -> None:
        out = git_tools.show_diff(repo.root, repo.first, repo.second, max_bytes=40)
        assert out.splitlines()[-1].startswith("[truncated: ")
        assert "bytes dropped, limit 40 bytes]" in out


@needs_rg
class TestSearch:
    def test_finds_matches_with_path_and_line(self, repo: Repo) -> None:
        out = git_tools_search(repo, "greet")
        assert out == "src/app.py:1:def greet():\n"

    def test_glob_limits_the_search(self, repo: Repo) -> None:
        assert git_tools_search(repo, "demo", glob="*.py") == ""
        assert git_tools_search(repo, "demo", glob="*.md") == "README.md:1:# demo\n"

    def test_no_match_is_empty(self, repo: Repo) -> None:
        assert git_tools_search(repo, "zzz-not-there") == ""

    def test_bad_regex_is_an_input_error(self, repo: Repo) -> None:
        with pytest.raises(ToolInputError, match="rg rejected"):
            git_tools_search(repo, "(unclosed")

    def test_empty_pattern_is_an_input_error(self, repo: Repo) -> None:
        with pytest.raises(ToolInputError):
            git_tools_search(repo, "")

    def test_never_reads_through_symlinks_out_of_the_tree(self, repo: Repo) -> None:
        assert SECRET not in git_tools_search(repo, "TOP-SECRET")
        assert git_tools_search(repo, "TOP-SECRET") == ""

    def test_pattern_starting_with_a_dash_is_a_pattern(self, repo: Repo) -> None:
        assert git_tools_search(repo, "--output") == ""

    def test_truncates_with_a_marker(self, repo: Repo) -> None:
        out = rg_tools.search(repo.root, "e", max_bytes=12)
        assert out.splitlines()[-1].startswith("[truncated: ")
        assert "limit 12 bytes]" in out


def git_tools_search(repo: Repo, pattern: str, glob: str | None = None) -> str:
    """Search the fixture repository with `rg.search`."""
    return rg_tools.search(repo.root, pattern, glob)


class TestRepoRoot:
    def test_missing_directory_is_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ToolNotFoundError, match="does not exist"):
            git_tools.read_file(tmp_path / "gone", "a", "HEAD")

    def test_plain_directory_is_not_a_work_tree(self, tmp_path: Path) -> None:
        with pytest.raises(ToolInputError, match="not a git work tree"):
            git_tools.read_file(tmp_path, "a", "HEAD")

    def test_subdirectory_is_not_the_top(self, repo: Repo) -> None:
        with pytest.raises(ToolInputError, match="top of a git work tree"):
            git_tools.read_file(repo.root / "src", "app.py", "HEAD")

    def test_missing_git_is_a_typed_error(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "")
        with pytest.raises(ToolUnavailableError, match="git"):
            git_tools.read_file(repo.root, "README.md", "HEAD")

    def test_missing_rg_is_a_typed_error(
        self, repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "only-git"
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(shutil.which("git") or "git")
        monkeypatch.setenv("PATH", str(bin_dir))
        with pytest.raises(ToolUnavailableError, match="rg"):
            rg_tools.search(repo.root, "greet")


class TestReadOnly:
    def test_no_tool_changes_the_repository(self, repo: Repo) -> None:
        before = (repo.git("status", "--porcelain"), repo.git("rev-parse", "HEAD"))
        listing = sorted(p.name for p in repo.root.rglob("*") if ".git" not in p.parts)
        git_tools.read_file(repo.root, "src/app.py", "HEAD")
        git_tools.show_diff(repo.root, repo.first, repo.second)
        if shutil.which("rg"):
            rg_tools.search(repo.root, "greet")
        after = (repo.git("status", "--porcelain"), repo.git("rev-parse", "HEAD"))
        assert before == after
        assert listing == sorted(p.name for p in repo.root.rglob("*") if ".git" not in p.parts)


class TestTruncate:
    def test_short_output_is_unchanged(self) -> None:
        assert truncate(b"abc\n", 4) == "abc\n"

    def test_marker_is_one_line_with_the_dropped_count(self) -> None:
        assert truncate(b"abcdef", 4) == "abcd\n[truncated: 2 bytes dropped, limit 4 bytes]"

    def test_never_splits_a_multibyte_character(self) -> None:
        data = "aé".encode() + b"zz"  # a, 0xC3 0xA9, z, z
        out = truncate(data, 2)
        assert out == "a\n[truncated: 4 bytes dropped, limit 2 bytes]"
        assert "�" not in out

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            truncate(b"x", 0)
