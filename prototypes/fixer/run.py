"""Fix the `off-by-one` fixture end to end with option (a) of ADR 0001 and print what it cost.

Not production code: this is the throwaway evidence for `docs/decisions/0001-fixer-runtime.md`.
It builds the fixture into a fresh temporary directory, registers the prototype tools, runs the
unchanged `runtime.runner.run` on `agent/agent.yaml` with a recording hook where the action guard
of #23 would sit, writes the change as a patch file next to the repository, checks the fix in the
sandbox and confirms this repository's work tree did not change. Run it with
`uv run python -m prototypes.fixer.run`; it needs Docker, the sandbox image and
`ANTHROPIC_API_KEY`, and prints one JSON object.
"""

import argparse
import json
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.exit import FactoryError
from fixtures.build import build, discover
from prototypes.fixer.fixer_tools import register
from prototypes.fixer.sandbox import require_image, run_in_sandbox
from registry.resolver import load_spec
from registry.spec import LLMSpec
from runtime.hooks import ToolCall, ToolDecision, ToolResult
from runtime.runner import run
from runtime.trace import Trace
from tools._common import ToolExecutionError, fail_hint, git

FIXTURE = "off-by-one"
DEFECT_ID = "last-window-dropped"
MANIFEST = Path(__file__).parent / "agent" / "agent.yaml"
FACTORY_ROOT = Path(__file__).resolve().parents[2]

LAST_WINDOW_CHECK = (
    "python -c 'from series.rolling import rolling_sums; "
    "assert rolling_sums([1, 2, 3, 4], 2) == [3, 5, 7]'"
)
"""The defect's own example, run in the sandbox after the fix; it fails on the unfixed code."""

TEST_SUITE = "python -m pytest -q"


@dataclass
class RecordingHook:
    """A `runtime.hooks.ToolHook` that allows every call and records it with its duration.

    It stands where the action guard of #23 attaches: `before_tool` sees the dotted name and the
    arguments of every call before it runs, including each `fs.write_file` path and each
    `shell.run` command, and could deny it. File contents are left out of the record.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)
    _started: float = 0.0

    def before_tool(self, call: ToolCall) -> ToolDecision:
        """Allow the call and start its clock."""
        self._started = time.monotonic()
        return ToolDecision.allow()

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        """Record the call's name, arguments without file contents, error flag and seconds."""
        self.calls.append(
            {
                "tool": call.name,
                "arguments": {k: v for k, v in call.arguments.items() if k != "content"},
                "is_error": result.is_error,
                "seconds": round(time.monotonic() - self._started, 2),
            }
        )


def finding_payload() -> dict[str, Any]:
    """Return the fixer's input for the fixture's one defect, taken from its `defects.yaml`."""
    manifest = discover()[FIXTURE].load_manifest()
    defect = next(d for d in manifest.defects if d.id == DEFECT_ID)
    return {
        "finding": {
            "file": defect.file,
            "line_start": defect.line_start,
            "line_end": defect.line_end,
            "title": defect.title,
            "description": defect.description,
        }
    }


def write_patch(repo: Path, patch: Path) -> list[str]:
    """Write the work tree's change against HEAD, new files included, to `patch`.

    No program the repository configures runs: `tools._common.git` turns off fsmonitor, and the
    diff turns off external diff drivers and textconv. Returns the changed paths.
    """
    _git_ok(["add", "--all"], repo)
    patch.write_bytes(
        _git_ok(["diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"], repo)
    )
    return _git_ok(["diff", "--cached", "--name-only", "HEAD"], repo).decode("utf-8").split()


def _git_ok(args: list[str], repo: Path) -> bytes:
    """Return the stdout of `git args` in `repo`, raising `ToolExecutionError` if it fails."""
    proc = git(args, repo)
    if proc.returncode != 0:
        raise ToolExecutionError(f"git {args[0]} failed in {repo}", hint=fail_hint(proc))
    return proc.stdout


def factory_status() -> bytes:
    """Return `git status --porcelain` of this repository, to show the run changed nothing here."""
    return git(["status", "--porcelain", "--untracked-files=all"], FACTORY_ROOT).stdout


def fix(dest: Path) -> dict[str, Any]:
    """Build the fixture under `dest`, run the fixer on it, and return the evidence as a dict.

    A failed run is evidence too: its error lands in the result, under `error`, instead of being
    raised. Docker, the image and the API key are checked by the calls that need them.
    """
    require_image()
    register()
    spec = load_spec(MANIFEST)
    assert isinstance(spec, LLMSpec)
    before = factory_status()
    repo = build([FIXTURE], dest=dest)[0].path
    hook = RecordingHook()
    pipeline_run_id = f"fixer-proto-{uuid.uuid4()}"
    report: dict[str, Any] | None = None
    error: str | None = None
    start = time.monotonic()
    with Trace(dest / "trace.duckdb") as trace:
        try:
            output = run(
                spec,
                finding_payload(),
                trace,
                repo_path=repo,
                pipeline_run_id=pipeline_run_id,
                hook=hook,
            )
            report = output.model_dump(mode="json")
        except FactoryError as exc:
            error = f"{type(exc).__name__}: {exc}"
        seconds = time.monotonic() - start
        row = (
            trace._connect()
            .execute(
                "SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "cost_usd FROM traces WHERE pipeline_run_id = ?",
                [pipeline_run_id],
            )
            .fetchone()
        )
    patch = dest / f"{FIXTURE}.patch"
    files_changed = write_patch(repo, patch)
    checks = {
        "last_window": run_in_sandbox(repo, LAST_WINDOW_CHECK).exit_code,
        "test_suite": run_in_sandbox(repo, TEST_SUITE).exit_code,
    }
    shell_runs = [c for c in hook.calls if c["tool"] == "shell.run"]
    input_tokens, output_tokens, cache_read, cache_write, cost = row or (None,) * 5
    return {
        "fixture": FIXTURE,
        "model": spec.model,
        "effort": spec.effort,
        "fixed": error is None and all(code == 0 for code in checks.values()),
        "error": error,
        "checks": checks,
        "agent_seconds": round(seconds, 1),
        "cost_usd": cost,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
        },
        "tool_calls": len(hook.calls),
        "tool_errors": sum(c["is_error"] for c in hook.calls),
        "shell_runs": len(shell_runs),
        "shell_seconds": round(sum(c["seconds"] for c in shell_runs), 1),
        "commands": [c["arguments"].get("command") for c in shell_runs],
        "writes": [c["arguments"].get("path") for c in hook.calls if c["tool"] == "fs.write_file"],
        "files_changed": files_changed,
        "patch": str(patch),
        "factory_tree_unchanged": factory_status() == before,
        "report": report,
    }


def main(argv: list[str] | None = None) -> int:
    """Run `fix` in `--dest` or a new temporary directory, print its JSON, and exit 0 if fixed."""
    parser = argparse.ArgumentParser(prog="python -m prototypes.fixer.run", description=__doc__)
    parser.add_argument("--dest", type=Path, help="directory to build into (default: a new one)")
    args = parser.parse_args(argv)
    dest = args.dest or Path(tempfile.mkdtemp(prefix="fixer-proto-"))
    result = fix(dest.resolve())
    print(json.dumps(result, indent=2))
    return 0 if result["fixed"] else 1


if __name__ == "__main__":
    sys.exit(main())
