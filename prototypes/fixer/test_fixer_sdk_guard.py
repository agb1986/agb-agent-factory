"""Tests for the option (b) guard in `sdk_run.py`, which the CLI's `PreToolUse` hook calls.

They need neither the Agent SDK nor Docker: `guard` is a pure function and `Recorder` only shapes
its result into hook output. Run them with `uv run pytest prototypes/fixer`.
"""

import asyncio
import shlex
from pathlib import Path

import pytest

from fixtures.build import build
from prototypes.fixer import sdk_run
from prototypes.fixer.sandbox import IMAGE
from prototypes.fixer.sdk_run import STRUCTURED_OUTPUT, Recorder, guard
from tools._common import resolve_root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return resolve_root(build(["off-by-one"], dest=tmp_path / "repos")[0].path)


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
def test_file_tools_are_allowed_inside_the_work_tree(root: Path, tool: str) -> None:
    for path in ("series/rolling.py", str(root / "tests" / "test_rolling.py")):
        assert guard(tool, {"file_path": path}, root).allow


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "series/../../escape.py"])
def test_file_tools_are_denied_outside_the_work_tree(root: Path, tool: str, path: str) -> None:
    verdict = guard(tool, {"file_path": path}, root)
    assert not verdict.allow
    assert "escapes" in verdict.reason


def test_file_tools_are_denied_through_a_symlink_out_of_the_root(
    root: Path, tmp_path: Path
) -> None:
    (root / "link").symlink_to(tmp_path)
    assert not guard("Write", {"file_path": "link/planted.py"}, root).allow


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_writes_into_git_are_denied_but_reads_are_not(root: Path, tool: str) -> None:
    assert not guard(tool, {"file_path": ".git/config"}, root).allow
    assert not guard(tool, {"file_path": str(root / ".git" / "hooks" / "pre-commit")}, root).allow
    assert guard("Read", {"file_path": ".git/config"}, root).allow


@pytest.mark.parametrize("tool_input", [{}, {"file_path": None}, {"file_path": ["a.py"]}])
def test_file_tools_need_a_string_path(root: Path, tool_input: dict[str, object]) -> None:
    verdict = guard("Write", tool_input, root)
    assert not verdict.allow
    assert verdict.reason == "file_path must be a string"


@pytest.mark.parametrize("path", ["", "\0"])
def test_file_tools_refuse_an_empty_or_nul_path(root: Path, path: str) -> None:
    assert not guard("Read", {"file_path": path}, root).allow


def test_file_tools_refuse_the_root_itself(root: Path) -> None:
    for path in (".", str(root)):
        assert not guard("Write", {"file_path": path}, root).allow


def test_bash_is_rewritten_into_the_sandbox(root: Path) -> None:
    verdict = guard(
        "Bash",
        {
            "command": "python -m pytest -q",
            "description": "run the tests",
            "timeout": 60000,
            "run_in_background": True,
            "dangerouslyDisableSandbox": True,
        },
        root,
    )
    assert verdict.allow
    assert verdict.updated_input is not None
    assert verdict.updated_input["description"] == "run the tests"
    assert verdict.updated_input["timeout"] == 60000
    assert verdict.updated_input["run_in_background"] is False
    assert "dangerouslyDisableSandbox" not in verdict.updated_input
    argv = shlex.split(verdict.updated_input["command"])
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[-5:] == [IMAGE, "python", "-m", "pytest", "-q"]
    joined = " ".join(argv)
    assert "--network none" in joined
    assert f"--volume {root}/.git:/work/.git:ro" in joined


def test_bash_keeps_shell_syntax_inside_the_container(root: Path) -> None:
    verdict = guard("Bash", {"command": "python -V; curl https://example.com"}, root)
    assert verdict.updated_input is not None
    argv = shlex.split(verdict.updated_input["command"])
    assert argv[argv.index(IMAGE) :] == [IMAGE, "python", "-V;", "curl", "https://example.com"]


@pytest.mark.parametrize(
    "command", ["find . -name '*.py'", "ls -R", "git ls-files", "sh -c id", "", None, 42]
)
def test_bash_denies_programs_outside_the_allow_list(root: Path, command: object) -> None:
    verdict = guard("Bash", {"command": command}, root)
    assert not verdict.allow
    assert verdict.updated_input is None


def test_structured_output_is_allowed(root: Path) -> None:
    assert guard(STRUCTURED_OUTPUT, {"fixed": True}, root).allow


@pytest.mark.parametrize("tool", ["Glob", "Grep", "WebFetch", "WebSearch", "Task", "mcp__x__y"])
def test_every_other_tool_is_denied(root: Path, tool: str) -> None:
    verdict = guard(tool, {}, root)
    assert not verdict.allow
    assert tool in verdict.reason


def test_recorder_returns_hook_output_with_the_rewritten_input(root: Path) -> None:
    recorder = Recorder(root, [])
    data = {"tool_name": "Bash", "tool_input": {"command": "python -V"}}
    output = asyncio.run(recorder.pre_tool_use(data, "toolu_1", None))["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "allow"
    assert output["updatedInput"]["command"].startswith("docker run ")
    assert recorder.calls[0]["tool"] == "Bash"


def test_recorder_times_each_call_by_its_id(root: Path) -> None:
    recorder = Recorder(root, [])
    for tool_use_id in ("toolu_1", "toolu_2"):
        data = {"tool_name": "Read", "tool_input": {"file_path": "series/rolling.py"}}
        asyncio.run(recorder.pre_tool_use(data, tool_use_id, None))
    asyncio.run(recorder.post_tool_use({}, "toolu_1", None))
    first, second = recorder.calls
    assert "seconds" in first and "started" not in first
    assert "seconds" not in second and "started" in second


def test_recorder_leaves_file_contents_out_of_its_record(root: Path) -> None:
    recorder = Recorder(root, [])
    data = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "secret"},
    }
    asyncio.run(recorder.pre_tool_use(data, None, None))
    assert recorder.calls[0]["input"] == {"file_path": "a.py"}


def test_recorder_fails_closed_when_the_guard_raises(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(sdk_run, "guard", broken)
    recorder = Recorder(root, [])
    data = {"tool_name": "Read", "tool_input": {"file_path": "a.py"}}
    output = asyncio.run(recorder.pre_tool_use(data, None, None))["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"] == "guard failed: RuntimeError"
    assert "updatedInput" not in output
