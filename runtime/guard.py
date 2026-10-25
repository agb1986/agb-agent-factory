"""The action guard: a tool hook that asks a guard agent before any tool that changes something.

`GuardHook` implements the `runtime.hooks.ToolHook` protocol. For a tool call it first checks
`tools.registry.READ_ONLY`; a read-only tool runs without a question and without a trace row. Every
other tool, and every name the registry does not know, is rendered into the guard agent's input
(tool name, arguments, the worktree root and a short task summary) and put to the guard. An allow
lets the call run. A deny returns a refusal that names the tool and the guard's reason, which the
runner sends to the model as an error tool result so it can adjust.

The third denial in one run ends the run: the hook raises `GuardError`, a `FactoryError`, which the
runner re-raises unchanged and records in the run's own trace row. A guard that cannot answer, for
example because its run failed validation, also raises `GuardError`: the call is never let through
on a guess.

Each decision is one trace row. Its `kind` and `agent_ref` are the guard agent's, and
`decision_json` holds `parent_run_id` (the run the call belongs to), `allow` and `reason`. When the
guard is an LLM agent the row is the guard run's own row, so it carries the tokens and the cost.

The guard agent comes from the `guard` field of the guarded agent's manifest, as `name@^MAJOR.MINOR`.
Two forms exist, chosen by the agent's `kind`: `llm`, the working form, and `jev`, a noul answering
"is this tool call safe inside the sandbox?". Turning a noul probability into allow or deny needs a
threshold, and thresholds live in `runtime/gates.py`, so `build_guard_hook` refuses the `jev` form
until that module exists.
"""

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from factory.exit import FactoryError, UsageError
from registry import resolve
from registry.spec import LLMSpec
from runtime import runner
from runtime.errors import AgentError
from runtime.hooks import ToolCall, ToolDecision, ToolHook, ToolResult
from runtime.trace import Trace, TraceRow
from tools.registry import READ_ONLY

MAX_DENIALS = 3
"""The number of denials in one run after which the guard ends the run."""


class GuardError(FactoryError):
    """The action guard ended the run (exit 1): too many denials, or a guard that cannot answer."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """A guard agent's answer: `allow`, the `reason`, and the guard run's own trace row, if any."""

    allow: bool
    reason: str
    row: TraceRow | None = None


class AskFailed(Exception):
    """The guard agent could not give an answer. `row` is the trace row of its failed run, if any."""

    def __init__(self, message: str, row: TraceRow | None = None) -> None:
        super().__init__(message)
        self.row = row


type Ask = Callable[[Mapping[str, Any]], Verdict]
"""Puts one rendered tool call to the guard agent and returns its verdict, or raises `AskFailed`."""


def render_call(call: ToolCall, *, worktree_root: str, task_summary: str) -> dict[str, Any]:
    """Return the guard agent's input for `call`: tool, arguments, worktree root and task summary."""
    return {
        "tool": call.name,
        "arguments": dict(call.arguments),
        "worktree_root": worktree_root,
        "task_summary": task_summary,
    }


class GuardHook:
    """A `ToolHook` that asks `ask` about every tool call that is not read-only.

    One instance is for one run: it counts that run's denials. `parent_run_id` is the id of the run
    being guarded and goes into every decision row. `guard_ref` and `guard_kind` are the guard
    agent's address and kind. `inner` is a hook the caller passed to the run; it is asked first, a
    denial from it is returned as it is and does not count, and it sees `after_tool`.
    """

    def __init__(
        self,
        ask: Ask,
        trace: Trace,
        *,
        parent_run_id: str,
        guard_ref: str,
        guard_kind: str = "llm",
        worktree_root: str,
        task_summary: str,
        pipeline_run_id: str | None = None,
        tag: str | None = None,
        inner: ToolHook | None = None,
        max_denials: int = MAX_DENIALS,
    ) -> None:
        self._ask = ask
        self._trace = trace
        self._parent_run_id = parent_run_id
        self._guard_ref = guard_ref
        self._guard_kind = guard_kind
        self._worktree_root = worktree_root
        self._task_summary = task_summary
        self._pipeline_run_id = pipeline_run_id
        self._tag = tag
        self._inner = inner
        self._max_denials = max_denials
        self.denials = 0

    def before_tool(self, call: ToolCall) -> ToolDecision:
        """Allow a read-only tool, or ask the guard; raise `GuardError` on the last denial."""
        if self._inner is not None:
            inner_decision = self._inner.before_tool(call)
            if not inner_decision.allowed:
                return inner_decision
        if call.name in READ_ONLY:
            return ToolDecision.allow()
        payload = render_call(
            call, worktree_root=self._worktree_root, task_summary=self._task_summary
        )
        started_at = datetime.now(UTC)
        start = time.monotonic()
        try:
            verdict = self._ask(payload)
        except AskFailed as exc:
            reason = f"the guard could not decide: {exc}"
            self._record(payload, Verdict(False, reason, exc.row), started_at, start, str(exc))
            raise GuardError(
                f"{self._guard_ref} could not decide on {call.name}: {exc}",
                hint="the call was not run; check the guard agent's trace row for the cause",
            ) from exc
        self._record(payload, verdict, started_at, start, None)
        if verdict.allow:
            return ToolDecision.allow()
        self.denials += 1
        if self.denials >= self._max_denials:
            raise GuardError(
                f"{self._guard_ref} refused {self.denials} tool calls; the last was {call.name}: "
                f"{verdict.reason}",
                hint="the run is ended; change what the agent is asked to do or the guard's rules",
            )
        return ToolDecision.deny(f"{call.name} refused by the action guard: {verdict.reason}")

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        """Pass the finished call on to the inner hook, if there is one."""
        if self._inner is not None:
            self._inner.after_tool(call, result)

    def _record(
        self,
        payload: Mapping[str, Any],
        verdict: Verdict,
        started_at: datetime,
        start: float,
        error: str | None,
    ) -> None:
        """Write the decision row: the guard run's own row when there is one, else a new one."""
        decision = {
            "parent_run_id": self._parent_run_id,
            "allow": verdict.allow,
            "reason": verdict.reason,
        }
        row = verdict.row
        if row is None:
            row = TraceRow(
                run_id=str(uuid.uuid4()),
                agent_ref=self._guard_ref,
                kind=self._guard_kind,
                started_at=started_at,
                duration_ms=int((time.monotonic() - start) * 1000),
                input_json=dict(payload),
                pipeline_run_id=self._pipeline_run_id,
                output_json=None if error else {"allow": verdict.allow, "reason": verdict.reason},
                error=error,
                tag=self._tag,
            )
        self._trace.record(replace(row, decision_json=decision))


class _Capture(Trace):
    """A `Trace` view that keeps the one row written to it instead of storing it."""

    def __init__(self, inner: Trace) -> None:
        super().__init__(inner.path)
        self.row: TraceRow | None = None

    def record(self, row: TraceRow) -> None:
        """Keep `row` for the guard hook, which writes it with the decision attached."""
        self.row = row


def llm_asker(
    spec: LLMSpec,
    trace: Trace,
    *,
    pipeline_run_id: str | None = None,
    tag: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> Ask:
    """Return an `Ask` that runs the `kind: llm` guard agent `spec` through `runtime.runner.run`.

    The guard run's own trace row is held back and returned in the verdict, so that the hook writes
    one row per decision. A run that fails with an `AgentError` raises `AskFailed`. Any other
    `FactoryError`, such as a missing API key, propagates unchanged.
    """

    def ask(payload: Mapping[str, Any]) -> Verdict:
        capture = _Capture(trace)
        try:
            output = runner.run(
                spec,
                payload,
                capture,
                pipeline_run_id=pipeline_run_id,
                client=client,
                tag=tag,
            )
        except AgentError as exc:
            raise AskFailed(f"{type(exc).__name__}: {exc}", capture.row) from exc
        data = output.model_dump()
        return Verdict(bool(data["allow"]), str(data["reason"]), capture.row)

    return ask


def build_guard_hook(
    spec: LLMSpec,
    trace: Trace,
    *,
    parent_run_id: str,
    repo_path: str | Path | None = None,
    pipeline_run_id: str | None = None,
    tag: str | None = None,
    inner: ToolHook | None = None,
    client: anthropic.Anthropic | None = None,
    agents_dir: Path | None = None,
) -> GuardHook:
    """Return the `GuardHook` for one run of `spec`, which must have a `guard` address.

    The address is resolved with `registry.resolve`. A `jev` guard raises `UsageError`: that form is
    documented but not wired. `repo_path` becomes the worktree root the guard is told about.
    """
    if spec.guard is None:
        raise UsageError(f"{spec.address} has no guard", hint="set `guard` in its manifest")
    name, _, version_range = spec.guard.partition("@")
    guard_spec = resolve(name, version_range, agents_dir)
    if not isinstance(guard_spec, LLMSpec):
        raise UsageError(
            f"{guard_spec.address} is a {guard_spec.kind} agent; only the llm guard form is wired",
            hint="point `guard` at an llm agent such as action-guard@^0.1",
        )
    root = str(Path(repo_path if repo_path is not None else Path.cwd()).resolve())
    return GuardHook(
        llm_asker(guard_spec, trace, pipeline_run_id=pipeline_run_id, tag=tag, client=client),
        trace,
        parent_run_id=parent_run_id,
        guard_ref=guard_spec.address,
        guard_kind=guard_spec.kind,
        worktree_root=root,
        task_summary=f"{spec.address}: {spec.description}",
        pipeline_run_id=pipeline_run_id,
        tag=tag,
        inner=inner,
    )
