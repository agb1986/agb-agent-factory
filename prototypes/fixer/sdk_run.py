"""Fix the `off-by-one` fixture with option (b) of ADR 0001, the Claude Agent SDK, for comparison.

Not production code: this is the runner-up's evidence for `docs/decisions/0001-fixer-runtime.md`.
The SDK drives the Claude Code CLI with its built-in `Read`, `Write`, `Edit` and `Bash` tools on
a fixture built into a fresh temporary directory. The CLI's own Bash sandbox needs `socat`, which
this host does not have, so every tool call goes through a `PreToolUse` hook instead: it denies a
path outside the work tree and rewrites each allowed `Bash` command into the same `docker run` that
option (a) uses, so no model-written command runs on the host. The CLI gets an empty config
directory and no setting sources, so this machine's Claude Code settings, hooks and plugins do not
load. The SDK is not a project dependency; run it with
`uv run --with claude-agent-sdk==0.2.174 python -m prototypes.fixer.sdk_run`, which needs Docker,
the sandbox image and `ANTHROPIC_API_KEY`, and prints one JSON object. `--probe-sandbox` instead
asks the CLI to start with its own sandbox required, and reports whether it can.
"""

import argparse
import asyncio
import json
import shlex
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fixtures.build import build
from prototypes.fixer.agent.schemas import FixReport
from prototypes.fixer.run import (
    FIXTURE,
    LAST_WINDOW_CHECK,
    MANIFEST,
    TEST_SUITE,
    factory_status,
    finding_payload,
    write_patch,
)
from prototypes.fixer.sandbox import docker_argv, parse_command, require_image, run_in_sandbox
from registry.resolver import load_spec
from registry.spec import LLMSpec
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace, TraceRow
from tools._common import ToolError, resolve_inside, resolve_root

SDK_VERSION = "0.2.174"
"""The `claude-agent-sdk` release this prototype ran against; it bundles Claude Code 2.1.310."""

TOOLS = ["Read", "Write", "Edit", "Bash"]
"""The CLI's built-in tools the fixer gets; every other tool is left out of its tool set."""

STRUCTURED_OUTPUT = "StructuredOutput"
"""The tool the CLI adds when `output_format` is set; the model calls it with the final report."""

MAX_BUDGET_USD = 1.0
"""Spend cap handed to the CLI. It is the CLI's own estimate, checked between turns."""

_CONTENT_KEYS = frozenset({"content", "old_string", "new_string"})
"""Tool input fields that carry file contents, which the record leaves out."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the guard decided for one tool call, and the input to run it with when allowed."""

    allow: bool
    reason: str
    updated_input: dict[str, Any] | None = None


def guard(tool: str, tool_input: dict[str, Any], root: Path) -> Verdict:
    """Decide one CLI tool call against the work tree at `root`, which `resolve_root` returned.

    `Read`, `Write` and `Edit` are allowed only on a path inside `root`, and the two that write
    only outside `.git`. `Bash` is allowed only for a command `sandbox.parse_command` accepts, and
    it is rewritten into the `docker run` that runs it in the sandbox. `StructuredOutput` is the
    CLI's own tool for `output_format` and runs nothing; denying it leaves the run without a report
    while the CLI still calls the run a success. Anything else is denied.
    """
    if tool == STRUCTURED_OUTPUT:
        return Verdict(True, "the final report")
    if tool in ("Read", "Write", "Edit"):
        path = tool_input.get("file_path")
        if not isinstance(path, str):
            return Verdict(False, "file_path must be a string")
        try:
            rel = resolve_inside(root, path)
        except ToolError as exc:
            return Verdict(False, str(exc))
        if rel == Path("."):
            return Verdict(False, "file_path must name a file in the repository")
        if tool != "Read" and rel.parts[0] == ".git":
            return Verdict(False, f"path is inside .git: {path!r}")
        return Verdict(True, "inside the work tree")
    if tool == "Bash":
        command = tool_input.get("command")
        try:
            argv = parse_command(command if isinstance(command, str) else "")
        except ToolError as exc:
            return Verdict(False, str(exc))
        name = f"fixer-proto-{uuid.uuid4().hex[:12]}"
        updated = {k: v for k, v in tool_input.items() if k != "dangerouslyDisableSandbox"}
        updated |= {
            "command": shlex.join(docker_argv(root, argv, name)),
            "run_in_background": False,
        }
        return Verdict(True, "rewritten to run in the Docker sandbox", updated)
    return Verdict(False, f"tool not available to the fixer: {tool}")


@dataclass
class Recorder:
    """The `PreToolUse` and `PostToolUse` hook callbacks, with a record of every call they saw.

    `pre_tool_use` is where the action guard of #23 would attach under option (b). A callback
    that raises is reported to the CLI as a hook error, which does not stop the tool, so any
    failure inside the guard is turned into a deny here.
    """

    root: Path
    calls: list[dict[str, Any]]

    async def pre_tool_use(self, data: Any, tool_use_id: str | None, context: Any) -> Any:
        """Return the guard's decision for one call, as `PreToolUse` hook output."""
        tool = data.get("tool_name", "")
        tool_input = data.get("tool_input") or {}
        try:
            verdict = guard(tool, tool_input, self.root)
        except Exception as exc:  # noqa: BLE001 - the guard fails closed, whatever went wrong
            verdict = Verdict(False, f"guard failed: {type(exc).__name__}")
        self.calls.append(
            {
                "tool": tool,
                "tool_use_id": tool_use_id,
                "input": {k: v for k, v in tool_input.items() if k not in _CONTENT_KEYS},
                "allowed": verdict.allow,
                "reason": verdict.reason,
                "started": time.monotonic(),
            }
        )
        output: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if verdict.allow else "deny",
            "permissionDecisionReason": verdict.reason,
        }
        if verdict.updated_input is not None:
            output["updatedInput"] = verdict.updated_input
        return {"hookSpecificOutput": output}

    async def post_tool_use(self, data: Any, tool_use_id: str | None, context: Any) -> Any:
        """Stamp the call `tool_use_id` names with its duration; the hook changes nothing.

        The model may ask for several tools in one turn, and a denied call gets no `PostToolUse`,
        so the call is found by its id rather than taken to be the latest one.
        """
        for call in self.calls:
            if call["tool_use_id"] == tool_use_id and "started" in call:
                call["seconds"] = round(time.monotonic() - call.pop("started"), 2)
        return {}


def _options(spec: LLMSpec, root: Path, config_dir: Path, recorder: Recorder | None) -> Any:
    """Return the `ClaudeAgentOptions` for one run on the work tree at `root`."""
    from claude_agent_sdk import (  # ty: ignore[unresolved-import]
        ClaudeAgentOptions,
        HookMatcher,
    )

    hooks = None
    if recorder is not None:
        hooks = {
            "PreToolUse": [HookMatcher(matcher=None, hooks=[recorder.pre_tool_use])],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[recorder.post_tool_use])],
        }
    return ClaudeAgentOptions(
        tools=TOOLS,
        system_prompt=(MANIFEST.parent / "system.md").read_text(),
        model=spec.model,
        thinking={"type": "adaptive"},
        effort=spec.effort,
        output_format={"type": "json_schema", "schema": FixReport.model_json_schema()},
        permission_mode="dontAsk",
        max_turns=spec.limits.max_tool_turns,
        max_budget_usd=MAX_BUDGET_USD,
        cwd=str(root),
        setting_sources=[],
        env={
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        hooks=hooks,
    )


async def _collect(prompt: str, options: Any) -> tuple[Any, list[str], str | None]:
    """Run one query; return its `ResultMessage`, the tool names asked for, and any error."""
    from claude_agent_sdk import (  # ty: ignore[unresolved-import]
        AssistantMessage,
        ClaudeSDKError,
        ResultMessage,
        ToolUseBlock,
        query,
    )

    result = None
    tool_uses: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                tool_uses += [b.name for b in message.content if isinstance(b, ToolUseBlock)]
            elif isinstance(message, ResultMessage):
                result = message
    except ClaudeSDKError as exc:
        return result, tool_uses, f"{type(exc).__name__}: {exc}"
    return result, tool_uses, None


def _usage(result: Any) -> TokenUsage:
    """Return the run's summed token usage from a `ResultMessage`."""
    usage = (result.usage if result is not None else None) or {}
    return TokenUsage(
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_write_tokens=usage.get("cache_creation_input_tokens"),
    )


def fix(dest: Path) -> dict[str, Any]:
    """Build the fixture under `dest`, run the SDK fixer on it, and return the evidence as a dict.

    The run is written to `dest/trace.duckdb` as one `TraceRow`, costed by `runtime.pricing`
    from the CLI's summed usage, next to the CLI's own `total_cost_usd` estimate.
    """
    require_image()
    spec = load_spec(MANIFEST)
    assert isinstance(spec, LLMSpec)
    before = factory_status()
    repo = resolve_root(build([FIXTURE], dest=dest)[0].path)
    config_dir = dest / "claude-config"
    config_dir.mkdir()
    recorder = Recorder(repo, [])
    payload = finding_payload()
    started_at = datetime.now(UTC)
    start = time.monotonic()
    result, tool_uses, error = asyncio.run(
        _collect(json.dumps(payload), _options(spec, repo, config_dir, recorder))
    )
    seconds = time.monotonic() - start
    report = getattr(result, "structured_output", None)
    if error is None and (result is None or result.is_error):
        error = f"run ended with {getattr(result, 'subtype', 'no result')}"
    if error is None:
        try:
            report = FixReport.model_validate(report).model_dump(mode="json")
        except ValidationError as exc:
            error = f"no valid structured output: {exc.error_count()} validation error(s)"
    usage = _usage(result)
    cost = cost_usd(spec.model, usage)
    with Trace(dest / "trace.duckdb") as trace:
        trace.record(
            TraceRow(
                run_id=str(uuid.uuid4()),
                agent_ref=f"{spec.address}+sdk",
                kind="llm",
                started_at=started_at,
                duration_ms=int(seconds * 1000),
                input_json=payload,
                pipeline_run_id=f"fixer-proto-sdk-{uuid.uuid4()}",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_usd=cost,
                output_json=report,
                error=error,
            )
        )
    patch = dest / f"{FIXTURE}.patch"
    files_changed = write_patch(repo, patch)
    checks = {
        "last_window": run_in_sandbox(repo, LAST_WINDOW_CHECK).exit_code,
        "test_suite": run_in_sandbox(repo, TEST_SUITE).exit_code,
    }
    bash = [c for c in recorder.calls if c["tool"] == "Bash"]
    return {
        "fixture": FIXTURE,
        "runtime": f"claude-agent-sdk {SDK_VERSION}",
        "model": spec.model,
        "effort": spec.effort,
        "fixed": error is None and all(code == 0 for code in checks.values()),
        "error": error,
        "checks": checks,
        "agent_seconds": round(seconds, 1),
        "cli_duration_api_ms": getattr(result, "duration_api_ms", None),
        "num_turns": getattr(result, "num_turns", None),
        "cost_usd": cost,
        "cli_total_cost_usd": getattr(result, "total_cost_usd", None),
        "tokens": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": usage.cache_read_tokens,
            "cache_write": usage.cache_write_tokens,
        },
        "model_usage": getattr(result, "model_usage", None),
        "tool_uses": tool_uses,
        "tool_calls": len(recorder.calls),
        "denied": [c for c in recorder.calls if not c["allowed"]],
        "permission_denials": getattr(result, "permission_denials", None),
        "bash_runs": len(bash),
        "bash_seconds": round(sum(c.get("seconds", 0.0) for c in bash), 1),
        "commands": [c["input"].get("command") for c in bash],
        "files_changed": files_changed,
        "patch": str(patch),
        "factory_tree_unchanged": factory_status() == before,
        "report": report,
    }


def probe_sandbox(dest: Path) -> dict[str, Any]:
    """Ask the CLI to start with its own Bash sandbox required, and report what happened.

    No tool can run: the fixer gets no tools and the prompt asks for one word. The point is only
    whether the CLI starts at all when `failIfUnavailable` is set and `socat` is missing.
    """
    from claude_agent_sdk import ClaudeAgentOptions  # ty: ignore[unresolved-import]

    spec = load_spec(MANIFEST)
    assert isinstance(spec, LLMSpec)
    config_dir = dest / "claude-config"
    config_dir.mkdir(parents=True)
    stderr: list[str] = []
    options = ClaudeAgentOptions(
        tools=[],
        model=spec.model,
        permission_mode="dontAsk",
        max_turns=1,
        cwd=str(dest),
        setting_sources=[],
        env={"CLAUDE_CONFIG_DIR": str(config_dir)},
        sandbox={
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
        },
        stderr=stderr.append,
    )
    result, _, error = asyncio.run(_collect("Reply with the single word OK.", options))
    return {
        "started": result is not None,
        "error": error,
        "result": getattr(result, "result", None),
        "cli_total_cost_usd": getattr(result, "total_cost_usd", None),
        "stderr": stderr[-10:],
    }


def main(argv: list[str] | None = None) -> int:
    """Run `fix`, or `probe_sandbox` with `--probe-sandbox`, and print its JSON."""
    parser = argparse.ArgumentParser(prog="python -m prototypes.fixer.sdk_run", description=__doc__)
    parser.add_argument("--dest", type=Path, help="directory to build into (default: a new one)")
    parser.add_argument("--probe-sandbox", action="store_true", help="only probe the CLI sandbox")
    args = parser.parse_args(argv)
    dest = (args.dest or Path(tempfile.mkdtemp(prefix="fixer-proto-sdk-"))).resolve()
    if args.probe_sandbox:
        print(json.dumps(probe_sandbox(dest), indent=2))
        return 0
    result = fix(dest)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["fixed"] else 1


if __name__ == "__main__":
    sys.exit(main())
