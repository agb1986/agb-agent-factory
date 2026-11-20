"""Outside writes: the one function pipeline code calls to change something outside the sandbox.

An outside write (a pull request, a review comment) is never handed to a model. Pipeline code calls
`write(name, arguments, agent=..., context=...)`, where `agent` is the resolved spec of the agent
the write is made for, and `context` is the pipeline's `OutsideContext`. The rules are in
`docs/decisions/0003-outside-writes.md` and docs/CONVENTIONS.md (Outside writes).

`write` runs `OutsideGuard`, a `ToolHook` whose checks are code, not an LLM. It allows the call only
when all of these hold:

- the tool is in `tools.registry.OUTSIDE_WRITES`;
- the call is made for `agent`, and `agent`'s manifest lists the tool in `tools.allow` and not in
  `tools.deny`;
- the `origin` of the context's repository is still the target;
- no earlier call of the same tool on the same target was written, dry-run or failed in this
  pipeline run, as the trace store shows. A refused call does not count, and neither does a call
  in another pipeline run: a rerun edits its earlier post through the marker instead.

A denial records the row and raises `OutsideWriteRefused` (exit 1), which ends the stage. An
allowed call runs the tool's impl with the context, which checks the target again itself.

Every call is one trace row, whatever happens; the layout is in docs/CONVENTIONS.md. `outcome` is
`written`, or `dry_run` under `context.dry_run`; `refused` when the guard or the tool refuses, or
the arguments do not fit the tool, all before anything is sent; and `failed` for any other error,
after which a part of the write may have reached GitHub. A failed call counts for the once rule,
so a pipeline never retries a half-made write by itself.

Callers: `factory build` (#87) and `factory hotfix` (#109) call `github.create_pr`, and
`factory review --pr N --comment` (#100) calls `github.comment_pr`, each for the resolved `judge`
manifest. When the judge gate skips the judge, the pipeline still resolves the `judge` manifest and
passes it as `agent`, because the manifest is the authority, not the run; `parent_run_id` is then
None. `notify.issue` (#95) will be called the same way for `escalation-summary`.
"""

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory.exit import UsageError
from registry.resolver import AgentSpec
from runtime.hooks import ToolCall, ToolDecision, ToolResult
from runtime.runner import _argument_problem
from runtime.trace import Trace, TraceRow
from tools._common import OutsideWriteRefused, ToolInputError, resolve_root
from tools.github import Repository, branch_for, origin_repository
from tools.registry import MAX_OUTPUT_BYTES, OUTSIDE_WRITES, SPECS

__all__ = ["TAG_PREFIX", "OutsideContext", "OutsideGuard", "OutsideWriteRefused", "write"]

TAG_PREFIX = "outside:"
"""Every outside write's trace row has the tag `outside:<tool>`."""

COUNTED_OUTCOMES = frozenset({"written", "dry_run", "failed"})
"""The outcomes of an earlier call that make a later one on the same target a repeat."""

_TARGET_KEYS = ("pr", "issue", "branch")


@dataclass(frozen=True, slots=True)
class OutsideContext:
    """What every outside write in one pipeline run shares.

    `target` is the github.com repository the pipeline was given, `repo_path` its local work tree,
    `pipeline_run_id` the pipeline's run id, `trace` the trace store, `dry_run` the pipeline's
    `--dry-run`, and `parent_run_id` the authorising agent's `run_id` in this pipeline, or None when
    it did not run. Build one with `for_repository` when the pipeline starts.
    """

    target: Repository
    repo_path: Path
    pipeline_run_id: str
    trace: Trace
    dry_run: bool = False
    parent_run_id: str | None = None

    @classmethod
    def for_repository(
        cls,
        repo_path: str | Path,
        *,
        pipeline_run_id: str,
        trace: Trace,
        dry_run: bool = False,
        parent_run_id: str | None = None,
    ) -> OutsideContext:
        """Return the context for the repository at `repo_path`, its target read from `origin`.

        Raises `ToolInputError`, a `UsageError` (exit 2), when `origin` is missing or not on
        github.com.
        """
        root = resolve_root(repo_path)
        return cls(
            target=origin_repository(root),
            repo_path=root,
            pipeline_run_id=pipeline_run_id,
            trace=trace,
            dry_run=dry_run,
            parent_run_id=parent_run_id,
        )


def planned_target(name: str, context: OutsideContext, arguments: Mapping[str, Any]) -> dict:
    """Return the trace row's `target` for a call before it runs: what the arguments name.

    The tool may add to it, such as the number of the pull request `github.create_pr` opened.
    """
    return {
        "repository": context.target.full_name,
        "pr": arguments.get("pr"),
        "issue": arguments.get("issue"),
        "branch": branch_for(context.pipeline_run_id) if name == "github.create_pr" else None,
    }


def _same_target(planned: Mapping[str, Any], earlier: Any) -> bool:
    """Return whether an earlier row's `target` names the target `planned` does."""
    if not isinstance(earlier, Mapping):
        return False
    if str(earlier.get("repository", "")).casefold() != planned["repository"].casefold():
        return False
    return all(earlier.get(key) == planned[key] for key in _TARGET_KEYS if planned[key] is not None)


class OutsideGuard:
    """The outside-write guard for one agent in one pipeline run; see the module docstring."""

    def __init__(self, context: OutsideContext, agent: AgentSpec) -> None:
        self._context = context
        self._agent = agent

    def before_tool(self, call: ToolCall) -> ToolDecision:
        """Allow `call` only when every check in the module docstring passes."""
        agent = self._agent
        if call.name not in OUTSIDE_WRITES:
            return ToolDecision.deny(f"{call.name} is not an outside write")
        if call.agent != agent.address:
            return ToolDecision.deny(f"the call is for {call.agent}, not {agent.address}")
        tools = getattr(agent, "tools", None)
        if (
            tools is None
            or call.name not in getattr(tools, "allow", ())
            or call.name in getattr(tools, "deny", ())
        ):
            return ToolDecision.deny(f"{agent.address} does not list {call.name} in tools.allow")
        context = self._context
        try:
            origin = origin_repository(context.repo_path)
        except UsageError as exc:
            return ToolDecision.deny(f"the repository has no github.com origin: {exc.message}")
        if not context.target.same(origin):
            return ToolDecision.deny(
                f"origin is {origin.full_name}, the target is {context.target.full_name}"
            )
        planned = planned_target(call.name, context, call.arguments)
        for row in context.trace.pipeline_runs(context.pipeline_run_id):
            decision = row.decision_json if isinstance(row.decision_json, Mapping) else {}
            if (
                row.tag == f"{TAG_PREFIX}{call.name}"
                and decision.get("outcome") in COUNTED_OUTCOMES
                and _same_target(planned, decision.get("target"))
            ):
                return ToolDecision.deny(
                    f"{call.name} already ran on this target in pipeline run "
                    f"{context.pipeline_run_id} (outcome {decision.get('outcome')})"
                )
        return ToolDecision.allow()

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        """Do nothing: `write` records the trace row."""


def write(
    name: str, arguments: Mapping[str, Any], *, agent: AgentSpec, context: OutsideContext
) -> dict[str, Any]:
    """Make the outside write `name` with `arguments` for `agent`, and return what it did.

    The result is `{"action": "create" | "edit", "url", "body"}`, `url` None in a dry run. Raises
    `OutsideWriteRefused` when the guard or the tool refuses, `ToolInputError` when the arguments
    do not fit the tool, and the tool's own error when it fails. One trace row is written in every
    case.
    """
    started_at = datetime.now(UTC)
    start = time.monotonic()
    arguments = dict(arguments)
    planned = planned_target(name, context, arguments)
    decision: dict[str, Any] = {
        "tool": name,
        "target": planned,
        "outcome": "refused",
        "allow": False,
        "reason": None,
        "dry_run": context.dry_run,
        "url": None,
    }
    output: dict[str, Any] | None = None
    error: BaseException | None = None
    try:
        verdict = OutsideGuard(context, agent).before_tool(
            ToolCall(name=name, arguments=arguments, agent=agent.address)
        )
        if not verdict.allowed:
            decision["reason"] = verdict.message
            raise OutsideWriteRefused(f"{name} refused: {verdict.message}")
        decision["allow"] = True
        problem = _argument_problem(SPECS[name].input_schema, arguments)
        if problem is not None:
            raise ToolInputError(f"{name}: {problem}")
        try:
            result = json.loads(SPECS[name].impl(context, **arguments, max_bytes=MAX_OUTPUT_BYTES))
        except OutsideWriteRefused, UsageError:
            raise
        except BaseException:
            decision["outcome"] = "failed"
            raise
        decision["target"] = {**planned, **result.pop("target", {})}
        decision["url"] = result.get("url")
        decision["outcome"] = "dry_run" if context.dry_run else "written"
        output = {"action": result["action"], "url": result["url"], "body": result["body"]}
        return output
    except BaseException as exc:
        error = exc
        if decision["reason"] is None:
            decision["reason"] = _message(exc)
        raise
    finally:
        context.trace.record(
            TraceRow(
                run_id=str(uuid.uuid4()),
                agent_ref=agent.address,
                kind="llm",
                started_at=started_at,
                duration_ms=int((time.monotonic() - start) * 1000),
                input_json=arguments,
                pipeline_run_id=context.pipeline_run_id,
                cost_usd=0.0,
                decision_json=decision,
                output_json=output,
                error=None if error is None else f"{type(error).__name__}: {_message(error)}",
                tag=f"{TAG_PREFIX}{name}",
                parent_run_id=context.parent_run_id,
            )
        )


def _message(exc: BaseException) -> str:
    """Return the message of `exc`, a `FactoryError`'s own `message` when it has one."""
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) else str(exc)
