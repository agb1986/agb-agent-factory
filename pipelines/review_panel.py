"""The review panel: reviewers read one change in parallel and the judge weighs their work.

`resolve_panel` pins the agents before anything runs, `run_panel` runs them under one pipeline run
id and `command` is what `factory review` calls. The pipeline decides nothing about the change
itself: the judge returns four scores, and `runtime.scoring` turns them into the weighted score and
the `ship` flag with the numbers in the judge's manifest. Before the reviewers run, `run_triage`
asks the Jev agent `diff-triage` about the diff and `runtime.gates.decide` turns its answers into a
`Decision`. While `runtime.gates.ROUTE_ONLY` holds, the decision is logged and every selected
reviewer still runs.
"""

import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from pipelines.ids import new_pipeline_run_id
from registry.common_schemas import ReviewInput, ReviewOutput
from registry.jev_spec import JevOutput, JevSpec
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.gates import ROUTE_ONLY, Decision, decide, jev_error
from runtime.jev_runner import run_jev
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

TRIAGE: tuple[str, str] = ("diff-triage", "^0.1")
"""The Jev agent that triages the diff and the version range it is pinned to."""

TRIAGE_TAG = "triage:decision"
"""The `tag` of the trace row that carries the triage `Decision`."""

type Runner = Callable[..., BaseModel]
type JevRunner = Callable[..., JevOutput]


class AllReviewersFailed(FactoryError):
    """Every reviewer of the panel failed, so there is nothing for the judge to weigh (exit 1)."""


@dataclass(frozen=True, slots=True)
class Panel:
    """The resolved agents of one run: the reviewers to run, in order, the judge and the triage.

    `triage` is `None` when triage is switched off (`--no-triage`).
    """

    reviewers: tuple[LLMSpec, ...]
    judge: LLMSpec
    triage: JevSpec | None = None


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
    """

    status: str
    spec: JevSpec | None
    decision: Decision | None
    probabilities: dict[str, Any] | None
    truncated: bool
    error: str | None

    def to_json(self) -> dict[str, Any]:
        """Return the `triage` entry of the pipeline document."""
        return {
            "status": self.status,
            "ref": self.spec.address if self.spec is not None else None,
            "decision": self.decision.to_json() if self.decision is not None else None,
            "probabilities": self.probabilities,
            "truncated": self.truncated,
            "route_only": ROUTE_ONLY,
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
        return f"{line}; route only, every reviewer ran" if ROUTE_ONLY else line


DISABLED = TriageResult("disabled", None, None, None, False, None)
"""The `TriageResult` of a run with triage switched off."""


@dataclass(frozen=True, slots=True)
class PanelResult:
    """Everything one panel run produced. `to_json` is the document `factory review` prints."""

    pipeline_run_id: str
    base: str
    head: str
    reviewers: tuple[ReviewerResult, ...]
    judge: LLMSpec
    judgement: BaseModel
    assessment: Assessment
    total_cost_usd: float
    duration_ms: int
    triage: TriageResult = DISABLED

    @property
    def ship(self) -> bool:
        """Whether the weighted score reached the judge's ship threshold."""
        return self.assessment.ship

    def to_json(self) -> dict[str, Any]:
        """Return the pipeline document: reviewers, judge, score, ship flag, cost and triage."""
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "base": self.base,
            "head": self.head,
            "reviewers": [reviewer.to_json() for reviewer in self.reviewers],
            "judge": {"ref": self.judge.address, "output": self.judgement.model_dump(mode="json")},
            "weighted_score": self.assessment.score,
            "ship": self.assessment.ship,
            "total_cost_usd": self.total_cost_usd,
            "duration_ms": self.duration_ms,
            "triage": self.triage.to_json(),
        }

    def text(self) -> str:
        """Return a short plain-text report of the run, for use without `--json`."""
        output = self.judgement.model_dump(mode="json")
        lines = [f"pipeline run {self.pipeline_run_id}: {self.base}..{self.head}"]
        lines.append(self.triage.text())
        for reviewer in self.reviewers:
            if reviewer.review is not None:
                found = len(reviewer.review.findings)
                lines.append(
                    f"  {reviewer.spec.address}: {reviewer.review.verdict}, {found} findings"
                )
            else:
                lines.append(f"  {reviewer.spec.address}: FAILED {reviewer.error}")
        for name, score in output["scores"].items():
            lines.append(f"  {name}: {score['value']:.2f}  {score['justification']}")
        for finding in output["merged_findings"]:
            where = f"{finding['file']}:{finding['line_start']}-{finding['line_end']}"
            lines.append(f"  [{finding['severity']}] {where} {finding['title']}")
        verdict = "ship" if self.ship else "do not ship"
        lines.append(f"{verdict}: weighted score {self.assessment.score:.2f}")
        lines.append(f"cost ${self.total_cost_usd:.4f}")
        return "\n".join(lines)


class _LockedTrace(Trace):
    """A view of a `Trace` whose writes are safe to make from the panel's worker threads.

    A `Trace` holds one DuckDB connection and is not thread-safe, so every write goes through the
    wrapped trace under a lock. The view itself never opens a connection.
    """

    def __init__(self, inner: Trace) -> None:
        super().__init__(inner.path)
        self._inner = inner
        self._lock = threading.Lock()

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
) -> Panel:
    """Resolve the judge, the selected reviewers and the triage agent, before anything runs.

    `reviewers` names agents of `REVIEWERS`; the default is all three. Unless `allow_unpublished`
    is set, only published versions resolve. `triage=False` leaves out `diff-triage`. Raises
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
    return Panel(reviewers=specs, judge=judge, triage=triage_spec)


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


_DIFF_HEADER = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)
_CUT_MARKER = re.compile(r"^\[truncated: \d+ bytes dropped, limit \d+ bytes\]$")


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
) -> TriageResult:
    """Ask the Jev agent `spec` about `diff` and turn its answers into a `Decision`.

    The Jev call writes its own trace row with the real cost under `pipeline_run_id`. This function
    adds one more, tagged `triage:decision`, with the decision and the probabilities. The input
    counts as truncated when the tool cut the diff or the Jev runner cut the text it sent. Any
    exception from the call, such as a missing key or a failed request, gives the escalating
    `jev_error` decision and the caller carries on; it is not raised.
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
        )
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
    assert result.decision is not None
    trace.record(
        TraceRow(
            run_id=str(uuid.uuid4()),
            agent_ref=spec.address,
            kind="jev",
            started_at=started_at,
            duration_ms=round((time.monotonic() - start) * 1000),
            input_json={"files": files},
            pipeline_run_id=pipeline_run_id,
            cost_usd=0.0,
            decision_json={
                **result.decision.to_json(),
                "route_only": ROUTE_ONLY,
                "truncated": truncated,
            },
            output_json=result.probabilities,
            error=error,
            tag=TRIAGE_TAG,
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
) -> PanelResult:
    """Review the change from `base` to `head` of the repository at `repo_path`.

    When the panel has a triage agent, `jev_runner` triages the diff first under the same
    `pipeline_run_id` and the `Decision` goes into the result and the trace. While `ROUTE_ONLY`
    holds the decision is information only and every selected reviewer runs. The reviewers run in
    a thread pool, and each `runner` call gets the same `pipeline_run_id`, as does the judge's, so
    the trace holds one row per agent under that id. A reviewer that raises is reported in the
    judge's input as failed, and the panel goes on, as it does when triage fails. Raises
    `ToolNotFoundError` for an unknown repository or ref, `FactoryError` (exit 1) when every
    reviewer failed, and whatever the judge's run raises.
    """
    if not ROUTE_ONLY:
        raise NotImplementedError("acting on a triage decision is not built yet, see #20")
    start = time.monotonic()
    run_id = pipeline_run_id or new_pipeline_run_id()
    diff = show_diff(repo_path, base, head, max_bytes=MAX_OUTPUT_BYTES)
    triaged = (
        run_triage(panel.triage, diff, trace, run_id, jev_runner=jev_runner)
        if panel.triage is not None
        else DISABLED
    )
    shared = _LockedTrace(trace)
    payload = ReviewInput(repo_path=str(repo_path), base=base, head=head).model_dump(mode="json")

    def review(spec: LLMSpec) -> ReviewerResult:
        try:
            output = runner(spec, payload, shared, repo_path=repo_path, pipeline_run_id=run_id)
            return ReviewerResult(spec, ReviewOutput.model_validate(output.model_dump()), None)
        except Exception as exc:  # noqa: BLE001 - a failed reviewer is reported, not fatal
            return ReviewerResult(spec, None, f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=len(panel.reviewers)) as pool:
        results = tuple(pool.map(review, panel.reviewers))
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
    judgement = runner(
        panel.judge,
        judge_input.model_dump(mode="json"),
        shared,
        repo_path=repo_path,
        pipeline_run_id=run_id,
    )
    return PanelResult(
        pipeline_run_id=run_id,
        base=base,
        head=head,
        reviewers=results,
        judge=panel.judge,
        judgement=judgement,
        assessment=assess(panel.judge, judgement),
        total_cost_usd=trace.cost_by_pipeline_run(run_id),
        duration_ms=round((time.monotonic() - start) * 1000),
        triage=triaged,
    )


def command(
    repo_path: str,
    *,
    base: str,
    head: str,
    as_json: bool,
    reviewers: Sequence[str] | None = None,
    allow_unpublished: bool = False,
    no_triage: bool = False,
) -> int:
    """Run `factory review`: review `base..head` of `repo_path` with the panel and report it.

    `no_triage` skips the Jev call. Triage needs `TYPESAFE_API_KEY` only to work: without it the
    run records a `jev_error` and the reviewers still run.

    Prose goes to stderr under `as_json`, where stdout gets exactly one JSON document, also when
    the change does not ship. Without it the text report goes to stdout. Returns 0 when the change
    ships and 1 when it does not.
    """
    panel = resolve_panel(reviewers, allow_unpublished=allow_unpublished, triage=not no_triage)
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(
            f"{API_KEY_ENV} is not set",
            hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
        )
    with Trace() as trace:
        result = run_panel(panel, repo_path, base, head, trace, runner=run, jev_runner=run_jev)
    if as_json:
        print(result.text(), file=sys.stderr)
        print(json.dumps(result.to_json()))
    else:
        print(result.text())
    return ExitCode.OK if result.ship else ExitCode.ERROR
