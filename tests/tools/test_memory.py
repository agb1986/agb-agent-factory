"""Tests for `memory.search` against a fake `agb` on PATH, and one live test of the real one."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from repo_helpers import Repo

from factory.exit import ExitCode, exit_code
from tools import ToolExecutionError, ToolInputError, ToolUnavailableError, memory, tool_defs
from tools.registry import READ_ONLY, SANDBOX_ONLY

ROWS = [
    {
        "backend": "claude-mem",
        "id": "5618",
        "title": "Created agents",
        "snippet": "Two complete\nagent implementations.",
        "timestamp": "2026-10-25T19:26:20.882Z",
    },
    {
        "backend": "ov",
        "id": "viking://resources/notes/plan",
        "title": "Plan",
        "snippet": "A plan.",
        "timestamp": None,
    },
]


def fake_agb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, code: int = 0) -> Path:
    """Put an `agb` on PATH that logs its arguments, prints `stdout` and exits with `code`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "stdout.txt"
    out.write_text(stdout)
    script = bin_dir / "agb"
    script.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done > "{tmp_path}/args.txt"\n'
        f'cat "{out}"\necho "agb: backend down" >&2\nexit {code}\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "args.txt"


def test_formats_each_row_with_source_and_ref(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_agb(tmp_path, monkeypatch, json.dumps(ROWS))
    assert memory.search(repo.root, "agents") == (
        "[claude-mem] claude-mem:5618 | 2026-10-25T19:26:20.882Z | Created agents\n"
        "  Two complete agent implementations.\n"
        "[ov] viking://resources/notes/plan | undated | Plan\n"
        "  A plan.\n"
    )


def test_runs_agb_read_only_with_the_limit_and_the_query_after_double_dash(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = fake_agb(tmp_path, monkeypatch, "[]")
    assert memory.search(repo.root, "--delete everything", 3) == ""
    assert args.read_text().split("\n")[:-1] == [
        "--json", "mem", "search", "--backend", "all", "--limit", "3", "--", "--delete everything",
    ]  # fmt: skip


def test_default_limit_is_ten(repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = fake_agb(tmp_path, monkeypatch, "[]")
    memory.search(repo.root, "x")
    assert args.read_text().split("\n")[6] == "10"


def test_output_is_capped(repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agb(tmp_path, monkeypatch, json.dumps(ROWS * 20))
    out = memory.search(repo.root, "x", max_bytes=200)
    assert out.endswith("limit 200 bytes]")
    assert "[truncated:" in out.splitlines()[-1]


@pytest.mark.parametrize("query", ["", "   ", "a\0b"])
def test_bad_query_is_refused(repo: Repo, query: str) -> None:
    with pytest.raises(ToolInputError):
        memory.search(repo.root, query)


@pytest.mark.parametrize("limit", [0, -1, memory.MAX_LIMIT + 1, True, "5"])
def test_bad_limit_is_refused(repo: Repo, limit: Any) -> None:
    with pytest.raises(ToolInputError, match="limit"):
        memory.search(repo.root, "x", limit)


def test_backend_down_is_unavailable(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_agb(tmp_path, monkeypatch, "", code=4)
    with pytest.raises(ToolUnavailableError) as err:
        memory.search(repo.root, "x")
    assert err.value.hint == "agb: backend down"
    assert exit_code(err.value) == ExitCode.AUTH_OR_CONFIG


def test_other_failure_is_an_execution_error(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_agb(tmp_path, monkeypatch, "", code=2)
    with pytest.raises(ToolExecutionError, match="exit code 2"):
        memory.search(repo.root, "x")


@pytest.mark.parametrize("stdout", ["not json", '{"a": 1}', "[1]", '[{"id": "1"}]'])
def test_unexpected_output_is_an_execution_error(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    fake_agb(tmp_path, monkeypatch, stdout)
    with pytest.raises(ToolExecutionError, match="not a result list"):
        memory.search(repo.root, "x")


def test_missing_agb_is_unavailable(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / "gitonly"
    git_dir.mkdir()
    (git_dir / "git").symlink_to(shutil.which("git") or "git")
    monkeypatch.setenv("PATH", str(git_dir))
    with pytest.raises(ToolUnavailableError, match="agb"):
        memory.search(repo.root, "x")


def test_is_read_only_and_not_sandbox_only() -> None:
    assert "memory.search" in READ_ONLY
    assert "memory.search" not in SANDBOX_ONLY
    (definition,) = tool_defs(["memory.search"])
    assert definition["name"] == "memory__search"
    assert set(definition["input_schema"]["properties"]) == {"query", "limit"}
    assert "sandbox" in definition["description"]


@pytest.mark.skipif(
    os.environ.get("FACTORY_LIVE") != "1" or shutil.which("agb") is None,
    reason="needs FACTORY_LIVE=1 and the real agb on PATH",
)
def test_live_agb_memory_search(repo: Repo) -> None:
    """The real `agb` returns rows this tool formats; a down backend is the only accepted failure."""
    try:
        out = memory.search(repo.root, "agent", 3)
    except ToolUnavailableError as exc:
        pytest.skip(f"memory backends are down: {exc}")
    probe = subprocess.run(
        ["agb", "--json", "mem", "search", "--backend", "all", "--limit", "3", "--", "agent"],
        capture_output=True,
        check=False,
    )
    rows = json.loads(probe.stdout)
    assert out.count("\n  ") == len(rows)
    for row in rows:
        assert f"[{row['backend']}] " in out
        assert str(row["id"]) in out
