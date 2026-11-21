"""The review panel: reviewers read one change in parallel and the judge weighs their work.

`resolve_panel` pins the agents before anything runs, `run_panel` runs them under one pipeline run
id and `command` is what `factory review` calls. The pipeline decides nothing about the change
itself: the judge returns four scores, and `runtime.scoring` turns them into the weighted score and
the `ship` flag with the numbers in the judge's manifest. Before the reviewers run, `run_triage`
asks the Jev agent `diff-triage` about the diff and `runtime.gates.decide` turns its answers into a
`Decision`. The decision is logged on every run. It is acted on only when the triage manifest sets
`act_on_decision` and the caller passes `act_on_triage=True`, which only `factory review` does: a
skip then runs no reviewer and no judge, and a narrower route runs only the reviewers it names.
`SKIP_SAMPLE_RATE` of the runs triage would skip still get the full panel, so a drift in what Jev
skips shows up as findings on sampled diffs. Every other caller, the fix, build and hotfix loops
included, runs every selected reviewer whatever the decision says.

Between the reviewers and the judge, `run_judge_gate` asks the Jev agent `judge-gate` whether the
reviews are clear-cut. When they are, `runtime.gates.decide_judge_gate` skips the judge and the
verdict comes from the gate; otherwise, and whenever anything is in doubt, the judge runs as it
always did.
"""

import asyncio
import dataclasses
import json
import os
import random
import re
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError, exit_code
from pipelines.ids import new_pipeline_run_id
from pipelines.review_comment import (
    comment_text,
    failure_section,
    post_review,
    resolve_pull_request,
)
from registry.common_schemas import ReviewInput, ReviewOutput
from registry.jev_spec import JevOutput, JevSpec, ProbabilityGate
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.async_runner import arun
from runtime.budget import DEFAULT_BUDGET_USD, Budget, BudgetExceeded
from runtime.gates import Decision, decide, decide_judge_gate, jev_error
from runtime.jev_runner import run_jev
from runtime.outside import OutsideContext
from runtime.pricing import TokenUsage, cost_usd
from runtime.runner import API_KEY_ENV, run
from runtime.scoring import Assessment, assess
from runtime.trace import Trace, TraceRow
from tools import MAX_OUTPUT_BYTES
from tools.git import show_diff

REVIEWERS: tuple[tuple[str, str], ...] = (
    ("code-reviewer", "^1.2"),
    ("security-reviewer", "^1.0"),
    ("perf-reviewer", "^0.3"),
)
"""The reviewer agents of the panel and the version range each is pinned to, in report order."""

JUDGE: tuple[str, str] = ("judge", "^2.0")
"""The judge agent and its version range."""

TRIAGE: tuple[str, str] = ("diff-triage", "^0.4")
"""The Jev agent that triages the diff and the version range it is pinned to."""

SKIP_SAMPLE_RATE = 0.05
"""Share of the runs triage would skip that get the full panel anyway, drawn at random.

A code constant and not a manifest value, so a manifest change cannot quietly switch the check off.
The sampled runs are how `scripts/shadow_report.py` keeps measuring the skip gate once it acts.
"""

OUTCOME_SKIPPED = "skipped"
"""`TriageResult.outcome` when triage skipped the review: no reviewer, judge gate or judge ran."""

OUTCOME_ROUTED = "routed"
"""`TriageResult.outcome` when only the reviewers on the route ran."""

OUTCOME_FULL = "full"
"""`TriageResult.outcome` when every selected reviewer ran."""

TRIAGE_TAG = "triage:decision"
"""The `tag` of the trace row that carries the triage `Decision`."""

JUDGE_GATE: tuple[str, str] = ("judge-gate", "^0.1")
"""The Jev agent that decides whether the judge can be skipped and the version range it is pinned to."""

JUDGE_GATE_TAG = "judge_gate:decision"
"""The `tag` of the trace row that carries the judge-gate `Decision` and the judge cost it saved."""

REVIEWER_FAILED = "reviewer_failed"
"""`Decision.reason` of the judge gate when a reviewer failed and no Jev call was made."""

JUDGE_OUTPUT_TOKENS_ESTIMATE = 1500
"""Output tokens assumed for a judge run when estimating what skipping it saved.

A judge reply is four scores with justifications plus the merged findings, which in the eval cases
runs 1,000 to 2,000 tokens. The saving is an estimate, not a measurement, and is labelled so.
"""

CHARS_PER_TOKEN = 4
"""Characters per token assumed when estimating the input of a judge run that did not happen."""

type Runner = Callable[..., BaseModel]
type JevRunner = Callable[..., JevOutput]
type Sampler = Callable[[], bool]


def draw_sample() -> bool:
    """Return true for `SKIP_SAMPLE_RATE` of calls: whether a would-skip run gets the full panel."""
    return random.random() < SKIP_SAMPLE_RATE


class AllReviewersFailed(FactoryError):
    """Every reviewer of the panel failed, so there is nothing for the judge to weigh (exit 1)."""


@dataclass(frozen=True, slots=True)
class Panel:
    """The resolved agents of one run: the reviewers to run, in order, the judge and the triage.

    `triage` is `None` when triage is switched off (`--no-triage`), and `gate`, the judge-gate
    agent, when the gate is (`--no-judge-gate`).
    """

    reviewers: tuple[LLMSpec, ...]
    judge: LLMSpec
    triage: JevSpec | None = None
    gate: JevSpec | None = None


@dataclass(frozen=True, slots=True)
class ReviewerResult:
    """How one reviewer went: its review, or the error that ended its run."""

    spec: LLMSpec
    review: ReviewOutput | None
    error: str | None

    def to_json(self) -> dict[str, Any]:
        """Return the reviewer's entry of the pipeline document."""
        return {
            "agent": self.spec.name,
            "version": self.spec.version,
            "ref": self.spec.address,
            "status": "ok" if self.review is not None else "failed",
            "verdict": self.review.verdict if self.review is not None else None,
            "finding_count": len(self.review.findings) if self.review is not None else None,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TriageResult:
    """How triage went: the `Decision`, the probabilities behind it, and whether it ran at all.

    `status` is `ok`, `jev_error` (the Jev call raised, so `error` holds why and the decision is
    the escalation) or `disabled` (`--no-triage`, nothing was called and `decision` is `None`).
    `route_only` is true unless the manifest sets `act_on_decision` and the caller allowed acting
    (`may_act`). `sampled` is true for a would-skip run drawn for the full panel. `outcome` is what
    the panel did: `skipped`, `routed` or `full`; `run_triage` alone leaves it `full`.
    """

    status: str
    spec: JevSpec | None
    decision: Decision | None
    probabilities: dict[str, Any] | None
    truncated: bool
    error: str | None
    may_act: bool = False
    route_only: bool = True
    sampled: bool = False
    outcome: str = OUTCOME_FULL

    @property
    def would_skip(self) -> bool:
        """Whether the decision is a skip the panel may honour: Jev answered, nothing escalated or
        was cut, and acting is allowed. A sampled run still would skip; `skips` leaves it out."""
        decision = self.decision
        return (
            not self.route_only
            and self.status == "ok"
            and decision is not None
            and decision.skip
            and not decision.escalate
            and not self.truncated
        )

    @property
    def skips(self) -> bool:
        """Whether the panel skips the review: a would-skip run that was not sampled."""
        return self.would_skip and not self.sampled

    def select(self, reviewers: tuple[LLMSpec, ...]) -> tuple[LLMSpec, ...]:
        """Return the reviewers to run, in panel order, out of `reviewers`.

        Only the ones on the route, when acting is allowed and the route names a proper subset of
        `reviewers`. Every one of them otherwise: route only, a failed or escalated or truncated
        triage, a sampled skip, an empty route, a route that names an agent outside `REVIEWERS`,
        and a route that covers them all or none of them.
        """
        decision = self.decision
        if (
            self.route_only
            or self.status != "ok"
            or decision is None
            or decision.escalate
            or self.truncated
            or self.sampled
            or not decision.route
        ):
            return reviewers
        known = {name for name, _ in REVIEWERS}
        if not set(decision.route) <= known:
            return reviewers
        chosen = tuple(spec for spec in reviewers if spec.name in decision.route)
        return chosen if 0 < len(chosen) < len(reviewers) else reviewers

    def skip_reason(self) -> str:
        """Return the gate that skipped and the probability it read, such as
        `low_risk: needs_review p=0.120`, for the reports of a skipped run."""
        assert self.decision is not None
        reason = self.decision.reason
        gate = next((g for g in self.spec.gates if g.name == reason), None) if self.spec else None
        answer = (self.probabilities or {}).get(gate.question) if gate is not None else None
        if not isinstance(gate, ProbabilityGate) or not isinstance(answer, dict):
            return reason
        p = answer.get("p") if gate.option is None else answer.get("probs", {}).get(gate.option)
        if not isinstance(p, int | float):
            return reason
        label = gate.question if gate.option is None else f"{gate.question}[{gate.option}]"
        return f"{reason}: {label} p={p:.3f}"

    def to_json(self) -> dict[str, Any]:
        """Return the `triage` entry of the pipeline document."""
        return {
            "status": self.status,
            "ref": self.spec.address if self.spec is not None else None,
            "decision": self.decision.to_json() if self.decision is not None else None,
            "probabilities": self.probabilities,
            "truncated": self.truncated,
            "route_only": self.route_only,
            "may_act": self.may_act,
            "sampled": self.sampled,
            "outcome": self.outcome,
            "error": self.error,
        }

    def text(self) -> str:
        """Return one line saying what triage decided, for the text reports."""
        if self.decision is None:
            return "triage: disabled"
        decision = self.decision
        route = ", ".join(decision.route) or "none"
        line = (
            f"triage: {decision.reason} (skip={decision.skip}, escalate={decision.escalate}, "
            f"route={route})"
        )
        if self.route_only:
            return f"{line}; route only, every reviewer ran"
        if self.outcome == OUTCOME_SKIPPED:
            return f"{line}; skipped the review, no reviewer ran"
        if self.sampled:
            return f"{line}; sampled, the full panel ran to check the skip"
        if self.outcome == OUTCOME_ROUTED:
            return f"{line}; only the reviewers on the route ran"
        return f"{line}; every reviewer ran"


DISABLED = TriageResult("disabled", None, None, None, False, None)
"""The `TriageResult` of a run with triage switched off."""

PATH_SKIPPED = "skipped_judge"
"""`GateResult.path` when the gate was sure and the judge did not run."""

PATH_ESCALATED = "escalated"
"""`GateResult.path` when the judge ran after the gate."""


@dataclass(frozen=True, slots=True)
class GateResult:
    """How the judge gate went: the `Decision`, the path taken and what skipping the judge saved.

    `status` is `ok`, `jev_error` (the Jev call raised, so `error` holds why and the decision is
    the escalation), `not_run` (a reviewer failed, so the gate escalated without a Jev call) or
    `disabled` (`--no-judge-gate`, nothing was called and `decision` is `None`). `path` is
    `skipped_judge` or `escalated`, and `None` when disabled. `verdict` is the gate's answer
    (`ship` or `request_changes`) and is only set when the judge was skipped.
    `judge_cost_estimate_usd` is an estimate of what the judge run that did not happen would have
    cost and is `None` unless the judge was skipped.
    """

    status: str
    spec: JevSpec | None
    decision: Decision | None
    probabilities: dict[str, Any] | None
    truncated: bool
    error: str | None
    path: str | None = None
    verdict: str | None = None
    judge_cost_estimate_usd: float | None = None

    @property
    def skipped(self) -> bool:
        """Whether the gate skipped the judge."""
        return self.path == PATH_SKIPPED

    def to_json(self) -> dict[str, Any]:
        """Return the `judge_gate` entry of the pipeline document."""
        return {
            "status": self.status,
            "ref": self.spec.address if self.spec is not None else None,
            "decision": self.decision.to_json() if self.decision is not None else None,
            "probabilities": self.probabilities,
            "truncated": self.truncated,
            "path": self.path,
            "verdict": self.verdict,
            "judge_cost_estimate_usd": self.judge_cost_estimate_usd,
            "error": self.error,
        }

    def text(self) -> str:
        """Return one line saying what the gate decided, for the text reports."""
        if self.status == GATE_TRIAGE_SKIPPED_STATUS:
            return "judge gate: not run, triage skipped the review"
        if self.decision is None:
            return "judge gate: disabled"
        if self.skipped:
            saved = self.judge_cost_estimate_usd
            note = f", saved about ${saved:.4f}" if saved is not None else ""
            return f"judge gate: skipped the judge, verdict {self.verdict}{note}"
        return f"judge gate: {self.decision.reason}, the judge ran"


GATE_DISABLED = GateResult("disabled", None, None, None, False, None)
"""The `GateResult` of a run with the judge gate switched off."""

GATE_TRIAGE_SKIPPED_STATUS = "triage_skipped"
"""`GateResult.status` when triage skipped the review, so there was nothing for the gate to read."""

GATE_TRIAGE_SKIPPED = GateResult(GATE_TRIAGE_SKIPPED_STATUS, None, None, None, False, None)
"""The `GateResult` of a run triage skipped."""


@dataclass(frozen=True, slots=True)
class PanelResult:
    """Everything one panel run produced. `to_json` is the document `factory review` prints."""

    pipeline_run_id: str
    base: str
    head: str
    reviewers: tuple[ReviewerResult, ...]
    judge: LLMSpec
    judgement: BaseModel | None
    assessment: Assessment | None
    total_cost_usd: float
    duration_ms: int
    triage: TriageResult = DISABLED
    gate: GateResult = GATE_DISABLED

    @property
    def skipped(self) -> bool:
        """Whether triage skipped the review, so that no reviewer, judge gate or judge ran."""
        return self.triage.outcome == OUTCOME_SKIPPED

    @property
    def ship(self) -> bool:
        """Whether the change ships: the weighted score reached the threshold, or the gate said so.

        When the gate skipped the judge there is no score, and the gate's verdict decides. When
        triage skipped the review nothing found a problem because nothing looked, and the change
        ships: that is what the skip gate is for, and `triage.outcome` says so.
        """
        if self.skipped:
            return True
        if self.assessment is not None:
            return self.assessment.ship
        return self.gate.verdict == "ship"

    def to_json(self) -> dict[str, Any]:
        """Return the pipeline document: reviewers, judge, score, ship flag, cost, triage and gate.

        When the gate skipped the judge, `judge` and `weighted_score` are null and `judge_gate`
        says why. When triage skipped the review, `reviewers` is empty, `judge` and
        `weighted_score` are null, `judge_gate.status` is `triage_skipped` and `triage.outcome` is
        `skipped`.
        """
        judge = (
            {"ref": self.judge.address, "output": self.judgement.model_dump(mode="json")}
            if self.judgement is not None
            else None
        )
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "base": self.base,
            "head": self.head,
            "reviewers": [reviewer.to_json() for reviewer in self.reviewers],
            "judge": judge,
            "weighted_score": self.assessment.score if self.assessment is not None else None,
            "ship": self.ship,
            "total_cost_usd": self.total_cost_usd,
            "duration_ms": self.duration_ms,
            "triage": self.triage.to_json(),
            "judge_gate": self.gate.to_json(),
        }

    def text(self) -> str:
        """Return a short plain-text report of the run, for use without `--json`."""
        lines = [f"pipeline run {self.pipeline_run_id}: {self.base}..{self.head}"]
        lines.append(self.triage.text())
        lines.append(self.gate.text())
        for reviewer in self.reviewers:
            if reviewer.review is not None:
                found = len(reviewer.review.findings)
                lines.append(
                    f"  {reviewer.spec.address}: {reviewer.review.verdict}, {found} findings"
                )
            else:
                lines.append(f"  {reviewer.spec.address}: FAILED {reviewer.error}")
        verdict = "ship" if self.ship else "do not ship"
        if self.skipped:
            lines.append(
                f"{verdict}: triage skipped the review ({self.triage.skip_reason()}); no reviewer "
                "or judge ran. Rerun with --no-triage to force a review"
            )
        elif self.judgement is not None and self.assessment is not None:
            output = self.judgement.model_dump(mode="json")
            for name, score in output["scores"].items():
                lines.append(f"  {name}: {score['value']:.2f}  {score['justification']}")
            for finding in output["merged_findings"]:
                where = f"{finding['file']}:{finding['line_start']}-{finding['line_end']}"
                lines.append(f"  [{finding['severity']}] {where} {finding['title']}")
            lines.append(f"{verdict}: weighted score {self.assessment.score:.2f}")
        else:
            lines.append(f"{verdict}: the judge gate was sure, no judge ran")
        lines.append(f"cost ${self.total_cost_usd:.4f}")
        return "\n".join(lines)


_RUN = run
"""The real runner. When the panel is given it, the reviewers run on `arun` on one event loop."""


class _LockedTrace(Trace):
    """A view of a `Trace` whose writes are safe to make from the reviewers' threads and loop.

    A plain `Trace` serialises its own calls, but the trace a caller passes in may be a subclass
    that does not, and a runner other than `run` writes from a worker thread. Every write goes
    through the wrapped trace under the view's lock. The view itself never opens a connection.
    """

    def __init__(self, inner: Trace) -> None:
        super().__init__(inner.path)
        self._inner = inner

    def record(self, row: TraceRow) -> None:
        """Write one run to the wrapped trace, one thread at a time."""
        with self._lock:
            self._inner.record(row)


class _CapturingTrace(Trace):
    """A view of a `Trace` that remembers the rows written through it.

    `run_jev` returns only the answers, and whether it cut the input to fit is in the `truncated`
    key of the `decision_json` of the row it writes. The view forwards each row to the wrapped
    trace and keeps a copy to read that key from.
    """

    def __init__(self, inner: Trace) -> None:
        super().__init__(inner.path)
        self._inner = inner
        self.rows: list[TraceRow] = []

    def record(self, row: TraceRow) -> None:
        """Write one run to the wrapped trace and keep it."""
        self._inner.record(row)
        self.rows.append(row)

    @property
    def truncated(self) -> bool:
        """Whether a Jev run written through this view cut its input."""
        return any(
            isinstance(row.decision_json, dict) and row.decision_json.get("truncated")
            for row in self.rows
        )


def resolve_panel(
    reviewers: Sequence[str] | None = None,
    *,
    allow_unpublished: bool = False,
    trace_path: Path | None = None,
    triage: bool = True,
    judge_gate: bool = True,
) -> Panel:
    """Resolve the judge, the selected reviewers, the triage agent and the judge gate.

    `reviewers` names agents of `REVIEWERS`; the default is all three. Unless `allow_unpublished`
    is set, only published versions resolve. `triage=False` leaves out `diff-triage` and
    `judge_gate=False` leaves out `judge-gate`, so the judge always runs. Raises
    `UsageError` for an empty or unknown name and the resolver's `NotFoundError` subclasses
    (exit 3) when an agent or version is missing.
    """
    known = dict(REVIEWERS)
    names = list(known) if reviewers is None else list(dict.fromkeys(reviewers))
    if not names:
        raise UsageError("--reviewers needs at least one name", hint=f"use {', '.join(known)}")
    unknown = [name for name in names if name not in known]
    if unknown:
        raise UsageError(
            f"unknown reviewer {', '.join(repr(name) for name in unknown)}",
            hint=f"use {', '.join(known)}",
        )
    specs = tuple(
        _resolve(name, known[name], allow_unpublished, trace_path)
        for name in known
        if name in names
    )
    judge = _resolve(*JUDGE, allow_unpublished, trace_path)
    triage_spec = (
        resolve_triage(allow_unpublished=allow_unpublished, trace_path=trace_path)
        if triage
        else None
    )
    gate_spec = (
        resolve_judge_gate(allow_unpublished=allow_unpublished, trace_path=trace_path)
        if judge_gate
        else None
    )
    return Panel(reviewers=specs, judge=judge, triage=triage_spec, gate=gate_spec)


def _resolve(
    name: str, version_range: str, allow_unpublished: bool, trace_path: Path | None
) -> LLMSpec:
    spec = resolve(name, version_range, published_only=not allow_unpublished, trace_path=trace_path)
    if not isinstance(spec, LLMSpec):
        raise UsageError(f"{spec.address} is not an LLM agent, so the panel cannot run it")
    return spec


def resolve_triage(*, allow_unpublished: bool = False, trace_path: Path | None = None) -> JevSpec:
    """Resolve the triage agent, `diff-triage` in the range `TRIAGE` pins.

    Raises `UsageError` when the name resolves to an agent that is not a Jev agent and the
    resolver's `NotFoundError` subclasses (exit 3) when it or the version is missing.
    """
    spec = resolve(*TRIAGE, published_only=not allow_unpublished, trace_path=trace_path)
    if not isinstance(spec, JevSpec):
        raise UsageError(f"{spec.address} is not a Jev agent, so triage cannot run it")
    return spec


def resolve_judge_gate(
    *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> JevSpec:
    """Resolve the judge-gate agent, `judge-gate` in the range `JUDGE_GATE` pins.

    Raises `UsageError` when the name resolves to an agent that is not a Jev agent and the
    resolver's `NotFoundError` subclasses (exit 3) when it or the version is missing.
    """
    spec = resolve(*JUDGE_GATE, published_only=not allow_unpublished, trace_path=trace_path)
    if not isinstance(spec, JevSpec):
        raise UsageError(f"{spec.address} is not a Jev agent, so the judge gate cannot run it")
    return spec


_DIFF_HEADER = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)
_CUT_MARKER = re.compile(r"^\[truncated: \d+ bytes dropped, limit \d+ bytes\]$")


def _lineage(parent_run_id: str | None) -> dict[str, str]:
    """The `parent_run_id` keyword for a runner call, or nothing when the panel has no parent."""
    return {} if parent_run_id is None else {"parent_run_id": parent_run_id}


def changed_files(diff: str) -> list[str]:
    """Return the paths a unified `diff` touches, as the `b/` side of each file header."""
    return list(dict.fromkeys(_DIFF_HEADER.findall(diff)))


def run_triage(
    spec: JevSpec,
    diff: str,
    trace: Trace,
    pipeline_run_id: str,
    *,
    jev_runner: JevRunner = run_jev,
    budget: Budget | None = None,
    parent_run_id: str | None = None,
    may_act: bool = False,
    sample: Sampler = draw_sample,
) -> TriageResult:
    """Ask the Jev agent `spec` about `diff` and turn its answers into a `Decision`.

    The Jev call writes its own trace row with the real cost under `pipeline_run_id`, and charges
    that cost to `budget` when one is given. This function adds one more row, tagged
    `triage:decision`, with the decision and the probabilities. The input counts as truncated when
    the tool cut the diff or the Jev runner cut the text it sent. Any exception from the call, such
    as a missing key or a failed request, gives the escalating `jev_error` decision and the caller
    carries on; it is not raised. `BudgetExceeded` is the one exception: it propagates, because a
    panel that is out of money must stop rather than go on to the reviewers.

    `may_act` says whether the caller will act on the decision. The result is route only unless
    it is true and the manifest sets `act_on_decision`. When acting would skip the review, `sample`
    is called once and a true answer marks the run `sampled`, for the full panel. The row records
    `route_only`, `may_act` and `sampled` beside the decision.
    """
    started_at = datetime.now(UTC)
    start = time.monotonic()
    files = changed_files(diff)
    capture = _CapturingTrace(trace)
    truncated = False
    output: JevOutput | None = None
    error: str | None = None
    try:
        output = jev_runner(
            spec,
            {"files": "\n".join(files), "diff": diff},
            capture,
            pipeline_run_id=pipeline_run_id,
            budget=budget,
            **_lineage(parent_run_id),
        )
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed triage escalates, it does not stop the panel
        error = f"{type(exc).__name__}: {exc}"
    if output is None:
        result = TriageResult("jev_error", spec, jev_error(), None, False, error)
    else:
        last_line = diff.rstrip("\n").rpartition("\n")[2]
        truncated = capture.truncated or bool(_CUT_MARKER.match(last_line))
        result = TriageResult(
            "ok",
            spec,
            decide(spec, output, truncated),
            output.model_dump(mode="json"),
            truncated,
            None,
        )
    decision = result.decision
    assert decision is not None
    route_only = not (may_act and spec.act_on_decision)
    result = dataclasses.replace(result, may_act=may_act, route_only=route_only)
    if result.would_skip:
        result = dataclasses.replace(result, sampled=sample())
    trace.record(
        TraceRow(
            run_id=str(uuid.uuid4()),
            agent_ref=spec.address,
            kind="jev",
            started_at=started_at,
            duration_ms=round((time.monotonic() - start) * 1000),
            input_json={"files": files},
            pipeline_run_id=pipeline_run_id,
            parent_run_id=parent_run_id,
            cost_usd=0.0,
            decision_json={
                **decision.to_json(),
                "route_only": result.route_only,
                "may_act": result.may_act,
                "sampled": result.sampled,
                "truncated": truncated,
            },
            output_json=result.probabilities,
            error=error,
            tag=TRIAGE_TAG,
        )
    )
    return result


def render_reviews(results: Sequence[ReviewerResult]) -> str:
    """Render the reviewers' work as the `reviews` text the judge-gate agent reads.

    One block per reviewer, in panel order: its address, verdict and summary, then one line per
    finding as `[severity] file:start-end title`. A reviewer that failed gets a `FAILED` line.
    """
    blocks = []
    for result in results:
        if result.review is None:
            blocks.append(f"{result.spec.address}: FAILED {result.error}")
            continue
        review = result.review
        lines = [f"{result.spec.address}: {review.verdict}", f"summary: {review.summary}"]
        lines += [
            f"[{f.severity}] {f.file}:{f.line_start}-{f.line_end} {f.title}"
            for f in review.findings
        ]
        if not review.findings:
            lines.append("no findings")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def estimate_judge_cost(judge: LLMSpec, judge_input: BaseModel) -> float | None:
    """Estimate in USD what a judge run on `judge_input` would have cost, or `None` if unpriced.

    The input is the size of the judge's input JSON at `CHARS_PER_TOKEN` characters per token and
    the output is `JUDGE_OUTPUT_TOKENS_ESTIMATE`, priced with the judge model's entry in
    `runtime.pricing`. The judge never ran, so this is an estimate and is reported as one.
    """
    chars = len(judge_input.model_dump_json())
    usage = TokenUsage(
        input_tokens=chars // CHARS_PER_TOKEN, output_tokens=JUDGE_OUTPUT_TOKENS_ESTIMATE
    )
    return cost_usd(judge.model, usage)


def run_judge_gate(
    spec: JevSpec,
    judge: LLMSpec,
    results: Sequence[ReviewerResult],
    judge_input: BaseModel,
    trace: Trace,
    pipeline_run_id: str,
    *,
    jev_runner: JevRunner = run_jev,
    budget: Budget | None = None,
    parent_run_id: str | None = None,
) -> GateResult:
    """Ask the Jev agent `spec` whether the reviews are clear-cut enough to skip `judge`.

    The Jev call writes its own trace row with the real cost under `pipeline_run_id`, and charges
    that cost to `budget` when one is given. This function adds one more row, tagged
    `judge_gate:decision`, with the decision, the path taken and, when the judge is skipped, an
    estimate of the cost it would have had, which is how the trace shows the saving. When a
    reviewer failed the panel cannot call the reviews clear-cut, so the gate escalates without a
    Jev call (status `not_run`). Any exception from the call gives the escalating `jev_error`
    decision and the judge runs; only `BudgetExceeded` is raised, because a panel out of money
    must stop. The input counts as truncated when the Jev runner cut the text it sent.
    """
    started_at = datetime.now(UTC)
    start = time.monotonic()
    capture = _CapturingTrace(trace)
    output: JevOutput | None = None
    error: str | None = None
    truncated = False
    if any(result.review is None for result in results):
        status, decision = "not_run", Decision(False, True, (), REVIEWER_FAILED)
    else:
        try:
            output = jev_runner(
                spec,
                {"reviews": render_reviews(results)},
                capture,
                pipeline_run_id=pipeline_run_id,
                budget=budget,
                **_lineage(parent_run_id),
            )
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed gate escalates, it does not stop the panel
            error = f"{type(exc).__name__}: {exc}"
        truncated = capture.truncated
        if output is None:
            status, decision = "jev_error", jev_error()
        else:
            status, decision = "ok", decide_judge_gate(spec, output, truncated)
    answers = output.model_dump(mode="json") if output is not None else None
    skipped = decision.skip and answers is not None
    verdict = answers["verdict"]["answer"] if skipped and answers is not None else None
    estimate = estimate_judge_cost(judge, judge_input) if skipped else None
    result = GateResult(
        status,
        spec,
        decision,
        answers,
        truncated,
        error,
        path=PATH_SKIPPED if skipped else PATH_ESCALATED,
        verdict=verdict,
        judge_cost_estimate_usd=estimate,
    )
    trace.record(
        TraceRow(
            run_id=str(uuid.uuid4()),
            agent_ref=spec.address,
            kind="jev",
            started_at=started_at,
            duration_ms=round((time.monotonic() - start) * 1000),
            input_json={"reviewers": [r.spec.address for r in results]},
            pipeline_run_id=pipeline_run_id,
            parent_run_id=parent_run_id,
            cost_usd=0.0,
            decision_json={
                **decision.to_json(),
                "path": result.path,
                "judge_cost_estimate_usd": estimate,
                "truncated": truncated,
            },
            output_json=answers,
            error=error,
            tag=JUDGE_GATE_TAG,
        )
    )
    return result


def run_panel(
    panel: Panel,
    repo_path: str | Path,
    base: str,
    head: str,
    trace: Trace,
    *,
    runner: Runner = run,
    jev_runner: JevRunner = run_jev,
    pipeline_run_id: str | None = None,
    budget: Budget | None = None,
    parent_run_id: str | None = None,
    act_on_triage: bool = False,
    sample: Sampler = draw_sample,
) -> PanelResult:
    """Review the change from `base` to `head` of the repository at `repo_path`.

    When the panel has a triage agent, `jev_runner` triages the diff first under the same
    `pipeline_run_id` and the `Decision` goes into the result and the trace. Unless
    `act_on_triage` is true and the triage manifest sets `act_on_decision`, the decision is
    information only and every selected reviewer runs. When both hold, a skip that was not sampled
    (`sample`, see `run_triage`) ends the run there: no reviewer, judge gate or judge runs and the
    change ships, with `triage.outcome` `skipped`. A route that names a proper subset of the
    selected reviewers runs only those, and the judge gate and the judge see only them. Only
    `factory review` acts on triage; the fix, build and hotfix loops review diffs the factory
    wrote, so they leave `act_on_triage` off and every reviewer runs. The reviewers run at
    the same time under `asyncio.gather`: with the default `runner`, each on
    `runtime.async_runner.arun` on one event loop, and any other `runner` on a worker thread. Each
    call gets the same `pipeline_run_id`, as does the judge's, so the trace holds one row per agent
    under that id. `run_panel` runs that loop itself, so it is called from synchronous code, not
    from a coroutine. A reviewer that raises is reported in the
    judge's input as failed, and the panel goes on, as it does when triage fails, except one that
    ran out of budget: every run, triage's, the gate's and the judge's too, is charged to `budget`
    (default: `DEFAULT_BUDGET_USD`) and the first to pass it ends the panel with `BudgetExceeded`.
    `parent_run_id` is the run the panel follows, such as the fixer's in the fix loop: every row
    the panel writes names it as its parent, so the trace draws the panel under that run.
    When the panel has a judge-gate agent it runs after the reviewers, and when it is sure the
    judge is not run: `judgement` and `assessment` are then `None` and the gate's verdict decides
    `ship`. Raises
    `ToolNotFoundError` for an unknown repository or ref, `FactoryError` (exit 1) when every
    reviewer failed, and whatever the judge's run raises.
    """
    start = time.monotonic()
    run_id = pipeline_run_id or new_pipeline_run_id()
    diff = show_diff(repo_path, base, head, max_bytes=MAX_OUTPUT_BYTES)
    budget = budget or Budget(DEFAULT_BUDGET_USD)
    triaged = (
        run_triage(
            panel.triage,
            diff,
            trace,
            run_id,
            jev_runner=jev_runner,
            budget=budget,
            parent_run_id=parent_run_id,
            may_act=act_on_triage,
            sample=sample,
        )
        if panel.triage is not None
        else DISABLED
    )
    if triaged.skips:
        return PanelResult(
            pipeline_run_id=run_id,
            base=base,
            head=head,
            reviewers=(),
            judge=panel.judge,
            judgement=None,
            assessment=None,
            total_cost_usd=trace.cost_by_pipeline_run(run_id),
            duration_ms=round((time.monotonic() - start) * 1000),
            triage=dataclasses.replace(triaged, outcome=OUTCOME_SKIPPED),
            gate=GATE_TRIAGE_SKIPPED,
        )
    selected = triaged.select(panel.reviewers)
    if len(selected) < len(panel.reviewers):
        triaged = dataclasses.replace(triaged, outcome=OUTCOME_ROUTED)
    shared = _LockedTrace(trace)
    payload = ReviewInput(repo_path=str(repo_path), base=base, head=head).model_dump(mode="json")

    async def review(spec: LLMSpec) -> ReviewerResult:
        kwargs: dict[str, Any] = {
            "repo_path": repo_path,
            "pipeline_run_id": run_id,
            "budget": budget,
            **_lineage(parent_run_id),
        }
        try:
            if runner is _RUN:
                output = await arun(spec, payload, shared, **kwargs)
            else:
                output = await asyncio.to_thread(runner, spec, payload, shared, **kwargs)
            return ReviewerResult(spec, ReviewOutput.model_validate(output.model_dump()), None)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed reviewer is reported, not fatal
            return ReviewerResult(spec, None, f"{type(exc).__name__}: {exc}")

    async def review_all() -> list[ReviewerResult | BaseException]:
        # Every reviewer finishes before an error is raised, so each one's trace row and charge
        # are in before the panel ends; the first error in report order is the one raised.
        return await asyncio.gather(*(review(spec) for spec in selected), return_exceptions=True)

    outcomes = asyncio.run(review_all())
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    results = tuple(outcome for outcome in outcomes if isinstance(outcome, ReviewerResult))
    reviews = {r.spec.address: r.review for r in results if r.review is not None}
    if not reviews:
        errors = "; ".join(f"{r.spec.address}: {r.error}" for r in results)
        raise AllReviewersFailed(f"every reviewer failed: {errors}")

    judge_input = panel.judge.input_class.model_validate(
        {
            "repo_path": str(repo_path),
            "reviews": {address: r.model_dump() for address, r in reviews.items()},
            "failed_reviewers": [
                {"reviewer": r.spec.address, "error": r.error} for r in results if r.error
            ],
            "diff_context": {"base": base, "head": head, "diff": diff},
        }
    )
    gated = (
        run_judge_gate(
            panel.gate,
            panel.judge,
            results,
            judge_input,
            trace,
            run_id,
            jev_runner=jev_runner,
            budget=budget,
            parent_run_id=parent_run_id,
        )
        if panel.gate is not None
        else GATE_DISABLED
    )
    judgement: BaseModel | None = None
    assessment: Assessment | None = None
    if not gated.skipped:
        judgement = runner(
            panel.judge,
            judge_input.model_dump(mode="json"),
            shared,
            repo_path=repo_path,
            pipeline_run_id=run_id,
            budget=budget,
            **_lineage(parent_run_id),
        )
        assessment = assess(panel.judge, judgement)
    return PanelResult(
        pipeline_run_id=run_id,
        base=base,
        head=head,
        reviewers=results,
        judge=panel.judge,
        judgement=judgement,
        assessment=assessment,
        total_cost_usd=trace.cost_by_pipeline_run(run_id),
        duration_ms=round((time.monotonic() - start) * 1000),
        triage=triaged,
        gate=gated,
    )


def command(
    repo_path: str,
    *,
    base: str | None = None,
    head: str | None = None,
    as_json: bool,
    reviewers: Sequence[str] | None = None,
    allow_unpublished: bool = False,
    no_triage: bool = False,
    no_judge_gate: bool = False,
    budget_usd: float | None = None,
    pr: int | None = None,
    comment: bool = False,
    dry_run: bool = False,
) -> int:
    """Run `factory review`: review a change of `repo_path` with the panel and report it.

    The change is `base..head`, or pull request `pr` of the repository's `origin` on github.com:
    the merge base of its base branch and its head commit, read through `github.read_pr`, which
    needs `GH_TOKEN` and the commits in the local clone. `comment` then posts the judge's findings
    on the pull request through `github.comment_pr` (see `pipelines.review_comment`), and
    `dry_run` makes that a plan: every read runs, nothing is sent. `comment` and `dry_run` need
    `pr`, and `dry_run` needs `comment`. Everything that can fail before the models are called
    (the flags, `GH_TOKEN`, the pull request, the `origin`) is checked first, so a bad call costs
    nothing.

    `no_triage` skips the Jev call, so every selected reviewer runs. Otherwise the panel acts on
    the triage decision (see `run_panel`): a skipped change ships with no reviewer run, and the
    report and the comment say that triage skipped it and that `--no-triage` forces a review.
    Triage needs `TYPESAFE_API_KEY` only to work: without it the run records a `jev_error` and
    every reviewer runs. `no_judge_gate` skips the judge-gate
    call, so the judge always runs; the gate fails the same way, escalating to the judge.

    Prose goes to stderr under `as_json`, where stdout gets exactly one JSON document, also when
    the change does not ship. Without it the text report goes to stdout. The document's `comment`
    section is null without `comment`; when the write is refused or fails, the review is still
    reported, with the error in the section, and the exit code is the error's. Returns 0 when the
    change ships and 1 when it does not. `budget_usd` caps the spend of the whole run (default:
    `DEFAULT_BUDGET_USD`); passing it raises `BudgetExceeded`, exit 1, with the spend so far.
    """
    if pr is not None and (base is not None or head is not None):
        raise UsageError("--pr replaces --base and --head", hint="give one or the other")
    if pr is None and (base is None or head is None):
        raise UsageError("give --pr N, or both --base and --head")
    if (comment or dry_run) and pr is None:
        raise UsageError("--comment and --dry-run need --pr")
    if dry_run and not comment:
        raise UsageError("--dry-run needs --comment")
    panel = resolve_panel(
        reviewers,
        allow_unpublished=allow_unpublished,
        triage=not no_triage,
        judge_gate=not no_judge_gate,
    )
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(
            f"{API_KEY_ENV} is not set",
            hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
        )
    budget = Budget(DEFAULT_BUDGET_USD if budget_usd is None else budget_usd)
    pull = resolve_pull_request(repo_path, pr) if pr is not None else None
    if pull is not None:
        base, head = pull.base, pull.head
    assert base is not None and head is not None  # checked above, and set from the pull request
    comment_section: dict[str, Any] | None = None
    failure: FactoryError | None = None
    with Trace() as trace:
        context = (
            OutsideContext.for_repository(
                repo_path, pipeline_run_id=new_pipeline_run_id(), trace=trace, dry_run=dry_run
            )
            if comment
            else None
        )
        result = run_panel(
            panel,
            repo_path,
            base,
            head,
            trace,
            runner=run,
            jev_runner=run_jev,
            pipeline_run_id=context.pipeline_run_id if context is not None else None,
            budget=budget,
            act_on_triage=True,
        )
        if context is not None and pull is not None:
            try:
                comment_section = post_review(result, pull, context)
            except FactoryError as exc:
                comment_section = failure_section(exc)
                failure = exc
    text = result.text()
    if comment_section is not None:
        text += "\n" + comment_text(comment_section)
    document = {**result.to_json(), "comment": comment_section}
    if as_json:
        print(text, file=sys.stderr)
        print(json.dumps(document))
    else:
        print(text)
    if failure is not None:
        print(failure.message, file=sys.stderr)
        if failure.hint:
            print(f"hint: {failure.hint}", file=sys.stderr)
        return exit_code(failure)
    return ExitCode.OK if result.ship else ExitCode.ERROR
