"""Tests for pipelines/file_policy.py: an agent's `files` policy as a pipeline enforces it.

The fix loop's use of it, with the fixer's real manifest, is tested in test_fix_loop.py. These
tests use small policies of their own and a `refuse` that records what it was given.
"""

import subprocess
from pathlib import Path

import pytest

from pipelines.file_policy import FilePolicy, FilePolicyGuard, candidates, check_changed
from registry.spec import FilesSpec
from runtime.hooks import ToolCall


class Refused(Exception):
    pass


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, agent: str, path: str, reason: str, check: str) -> Exception:
        self.calls.append((agent, path, reason, check))
        return Refused(path)


CODER = FilePolicy(FilesSpec(allow=("src/**", "tests/**"), deny=("src/vendor/**",)))


def _write(clone: Path, path: str) -> ToolCall:
    return ToolCall("fs.write_file", {"path": path, "content": ""}, "coder@0.1.0")


def test_a_protected_path_gives_its_own_reason_before_the_manifest() -> None:
    policy = FilePolicy(FilesSpec(deny=("data/**",)), {"data/cases.json": "the run owns it"})
    assert policy.denial("data/cases.json") == "the run owns it"
    assert policy.denial("data/other.json") == "the path matches the files.deny pattern data/**"
    assert policy.denial("src/app.py") is None


def test_an_allow_list_refuses_what_it_does_not_name_and_deny_wins() -> None:
    assert CODER.denial("src/app.py") is None
    assert CODER.denial("tests/test_app.py") is None
    assert CODER.denial("README.md") == "the path matches no files.allow pattern"
    assert CODER.denial("src/vendor/lib.py") == (
        "the path matches the files.deny pattern src/vendor/**"
    )


def test_the_guard_refuses_a_denied_write_through_refuse(tmp_path: Path) -> None:
    refuse = Recorder()
    guard = FilePolicyGuard(tmp_path.resolve(), CODER, refuse)
    with pytest.raises(Refused):
        guard.before_tool(_write(tmp_path, "./README.md"))
    assert refuse.calls == [
        ("coder@0.1.0", "README.md", "the path matches no files.allow pattern", "hook")
    ]


def test_the_guard_checks_the_resolved_path_too(tmp_path: Path) -> None:
    clone = tmp_path.resolve()
    (clone / "src" / "vendor").mkdir(parents=True)
    (clone / "src" / "alias").symlink_to(clone / "src" / "vendor")
    refuse = Recorder()
    with pytest.raises(Refused):
        FilePolicyGuard(clone, CODER, refuse).before_tool(_write(clone, "src/alias/lib.py"))
    assert [call[1] for call in refuse.calls] == ["src/vendor/lib.py"]


@pytest.mark.parametrize(
    "call",
    [
        ToolCall("fs.write_file", {"path": "src/app.py"}, "coder@0.1.0"),
        ToolCall("fs.write_file", {"content": "no path"}, "coder@0.1.0"),
        ToolCall("fs.read_file", {"path": "README.md"}, "coder@0.1.0"),
        ToolCall("shell.run", {"argv": ["touch", "README.md"]}, "coder@0.1.0"),
    ],
)
def test_the_guard_allows_everything_else(call: ToolCall, tmp_path: Path) -> None:
    refuse = Recorder()
    assert FilePolicyGuard(tmp_path.resolve(), CODER, refuse).before_tool(call).allowed
    assert refuse.calls == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", ["src/app.py"]),
        ("./src/../src/app.py", ["src/app.py"]),
        ("../outside.py", []),
        (".", []),
    ],
)
def test_candidates_fold_the_path_and_drop_what_leaves_the_clone(
    path: str, expected: list[str], tmp_path: Path
) -> None:
    assert candidates(tmp_path.resolve(), path) == expected


def test_an_absolute_path_inside_the_clone_is_made_relative(tmp_path: Path) -> None:
    clone = tmp_path.resolve()
    assert candidates(clone, str(clone / "src" / "app.py")) == ["src/app.py"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("x = 1\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "base",
    )
    return repo


def test_check_changed_passes_a_change_the_policy_allows(clone: Path) -> None:
    (clone / "src" / "app.py").write_text("x = 2\n")
    refuse = Recorder()
    check_changed(clone, CODER, "coder@0.1.0", refuse)
    assert refuse.calls == []


def test_check_changed_refuses_a_denied_change_by_any_route(clone: Path) -> None:
    (clone / "README.md").write_text("written by a script\n")
    refuse = Recorder()
    with pytest.raises(Refused):
        check_changed(clone, CODER, "coder@0.1.0", refuse)
    assert refuse.calls == [
        ("coder@0.1.0", "README.md", "the path matches no files.allow pattern", "post-check")
    ]
