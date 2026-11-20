"""What a pipeline does once it has escalated: one call, right after `escalate` returns.

A stop path that writes an escalation record calls `after_escalation` once, with the record, the
run's `Budget`, its pipeline run id and its `OutsideContext`. It has two steps, in order:

1. The summary (#94): the escalation-summary agent writes one paragraph and the options from the
   record, the summary is checked against the record (`runtime.escalation_summary.ungrounded`)
   and stored with it (`runtime.escalations.store_summary`).
2. The issue post (#95): `runtime.notify.notify_escalation` posts the record, with the stored
   summary at the top when there is one, to the ticket's GitHub issue through the outside write
   `notify.issue`, made on behalf of the escalation-summary agent, which lists it in
   `tools.allow`.

A step added later goes here too, so every stop path gets it without a change of its own.

Nothing here may change how the run ends. A step that is skipped, or that fails, is reported in
the result with its reason, and the record stays as `escalate` wrote it: the item stays
`escalated`, the pipeline's exit code is the stop's, and the run still ends as an escalation.
`after_escalation` never raises an `Exception`. The rules are in `docs/CONVENTIONS.md`
(Escalations, Summaries, Posting to GitHub).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from factory.exit import NotFoundError
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.budget import Budget, BudgetExceeded
from runtime.escalation_summary import summary_input, ungrounded
from runtime.escalations import Escalation, get_summary, store_summary, summary_markdown
from runtime.notify import notify_escalation
from runtime.outside import OutsideContext
from runtime.runner import run
from runtime.trace import Trace

SUMMARY_AGENT: tuple[str, str] = ("escalation-summary", "^0.1")
"""The agent that summarises a record and the version range it is pinned to."""

Runner = Callable[..., BaseModel]


def resolve_summary_agent(
    *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> LLMSpec | None:
    """Resolve `SUMMARY_AGENT`, or return None when no version resolves or it is not an LLM agent.

    The summary is optional, so a pipeline resolves it next to its own agents and runs without
    summaries when this returns None. Unless `allow_unpublished` is set, only published versions
    resolve.
    """
    name, version_range = SUMMARY_AGENT
    try:
        spec = resolve(
            name, version_range, published_only=not allow_unpublished, trace_path=trace_path
        )
    except NotFoundError:
        return None
    return spec if isinstance(spec, LLMSpec) else None


@dataclass(frozen=True)
class Step:
    """How one step went: `done`, or not and why. `run_id` names the agent run, when one ran."""

    done: bool
    run_id: str | None = None
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the step as the JSON object a pipeline report carries."""
        return {"done": self.done, "run_id": self.run_id, "reason": self.reason}


@dataclass(frozen=True)
class Notify:
    """How the issue post went: `done` when a post was written, or not and why.

    `post` is `runtime.notify.notify_escalation`'s entry (`outcome`, `action`, `url`, `issue`,
    `error`, and `body` for a dry run's planned post), or None when the step was skipped before it
    tried.
    """

    done: bool
    post: dict[str, Any] | None = None
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the step as the JSON object a pipeline report carries."""
        return {"done": self.done, "post": self.post, "reason": self.reason}


@dataclass(frozen=True)
class AfterEscalation:
    """What `after_escalation` did, one entry per step."""

    summary: Step
    notify: Notify

    def to_json(self) -> dict[str, Any]:
        """Return the steps as the report's `after_escalation` object."""
        return {"summary": self.summary.to_json(), "notify": self.notify.to_json()}


def after_escalation(
    trace: Trace,
    record: Escalation,
    *,
    budget: Budget,
    pipeline_run_id: str | None,
    dry_run: bool,
    summary_agent: LLMSpec | None,
    outside: OutsideContext,
    runner: Runner = run,
) -> AfterEscalation:
    """Run the steps that follow an escalation and return what each one did. Never raises.

    The summary step runs `summary_agent` on `summary_input(record)` under `budget`, in
    `pipeline_run_id`, so its run lands in the trace with its cost and counts in the item's cost.
    The summary is stored only when it passes `ungrounded` against the record. It is skipped,
    with the reason in the result, in a dry run (nothing is written), for a `budget` stop (the
    money is gone), when `budget` has nothing left, and when `summary_agent` is None (no version
    of the agent resolved). An agent failure, a check that fails or a store that fails leaves the
    record without a summary and comes back as a step not done, with the run id when the agent
    ran.

    The notify step then reads the stored summary (`runtime.escalations.get_summary`) and posts
    the record to the ticket's issue through `outside`, the pipeline's outside-write context, for
    `summary_agent`; the issue shows the summary when there is one and the record alone when
    there is none, so a summary step that did not finish still leaves a post. The post costs no
    model money, so it runs after a `budget` stop too. It is skipped, with the reason, in a dry
    run and when `summary_agent` is None; with `outside.dry_run` set the entry is the planned
    post and nothing is sent. A post that is refused or fails is a step not done, with the entry.
    """
    summary = _summary(
        trace,
        record,
        budget=budget,
        pipeline_run_id=pipeline_run_id,
        dry_run=dry_run,
        agent=summary_agent,
        runner=runner,
    )
    notify = _notify(
        trace, record, dry_run=dry_run, agent=summary_agent, outside=outside, summary=summary
    )
    return AfterEscalation(summary=summary, notify=notify)


def _summary(
    trace: Trace,
    record: Escalation,
    *,
    budget: Budget,
    pipeline_run_id: str | None,
    dry_run: bool,
    agent: LLMSpec | None,
    runner: Runner,
) -> Step:
    if dry_run:
        return Step(False, reason="dry run: no summary is written")
    if record.trigger == "budget":
        return Step(False, reason="the run spent its budget, so no summary is written")
    if agent is None:
        return Step(False, reason="no published escalation-summary agent resolved")
    try:
        budget.check()
    except BudgetExceeded:
        return Step(False, reason="the budget has nothing left for a summary")
    run_id = str(uuid.uuid4())
    try:
        output = runner(
            agent,
            summary_input(record),
            trace,
            pipeline_run_id=pipeline_run_id,
            run_id=run_id,
            budget=budget,
        )
        summary = output.model_dump(mode="json")
        problems = ungrounded(summary, record.to_json())
        if problems:
            return Step(False, run_id, f"the summary failed its check: {'; '.join(problems)}")
        store_summary(trace, record.id, summary, run_id=run_id, agent_ref=agent.address)
    except Exception as exc:  # noqa: BLE001 - a failed summary leaves the record as it is
        return Step(False, run_id, f"{type(exc).__name__}: {exc}")
    return Step(True, run_id)


def _notify(
    trace: Trace,
    record: Escalation,
    *,
    dry_run: bool,
    agent: LLMSpec | None,
    outside: OutsideContext,
    summary: Step,
) -> Notify:
    if dry_run:
        return Notify(False, reason="dry run: nothing is posted")
    if agent is None:
        return Notify(False, reason="no published escalation-summary agent resolved")
    try:
        stored = get_summary(trace, record.id)
        text = summary_markdown(stored) if stored is not None else None
        context = replace(outside, parent_run_id=summary.run_id if summary.done else None)
        post = notify_escalation(trace, record.id, agent=agent, context=context, summary=text)
    except Exception as exc:  # noqa: BLE001 - a failed post leaves the record as it is
        return Notify(False, reason=f"{type(exc).__name__}: {exc}")
    if post["outcome"] == "written":
        return Notify(True, post)
    if post["outcome"] == "dry_run":
        return Notify(False, post, "dry run: the planned post is in the entry, nothing was sent")
    return Notify(False, post, f"the post was {post['outcome']}: {post['error']}")
