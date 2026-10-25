"""The review panel: reviewers read one change in parallel and the judge weighs their work.

`resolve_panel` pins the agents before anything runs, `run_panel` runs them under one pipeline run
id and `command` is what `factory review` calls. The pipeline decides nothing about the change
itself: the judge returns four scores, and `runtime.scoring` turns them into the weighted score and
the `ship` flag with the numbers in the judge's manifest.
"""

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from pipelines.ids import new_pipeline_run_id
from registry.common_schemas import ReviewInput, ReviewOutput
from registry.resolver import resolve
from registry.spec import LLMSpec
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

type Runner = Callable[..., BaseModel]


@dataclass(frozen=True, slots=True)
class Decision:
    """What a gate decided about a change: skip it, escalate it, or route it to named agents.

    This is the shape fixed in "Gates" in docs/CONVENTIONS.md. It lives here only until
    `runtime/gates.py` exists (#18), which takes it over; `pipelines.review_panel` then imports it
    from there.
    """

    skip: bool
    escalate: bool
    route: tuple[str, ...]
    reason: str


def triage(diff: str) -> Decision:
    """Decide how to review `diff` before any reviewer runs. For now it always reviews in full.

    This is a placeholder for the triage gate of #18. `run_panel` calls it and does not act on the
    answer yet, so every change goes to every selected reviewer.
    """
    return Decision(skip=False, escalate=False, route=(), reason="triage is not wired yet")


class AllReviewersFailed(FactoryError):
    """Every reviewer of the panel failed, so there is nothing for the judge to weigh (exit 1)."""


@dataclass(frozen=True, slots=True)
class Panel:
    """The resolved agents of one run: the reviewers to run, in order, and the judge."""

    reviewers: tuple[LLMSpec, ...]
    judge: LLMSpec


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

    @property
    def ship(self) -> bool:
        """Whether the weighted score reached the judge's ship threshold."""
        return self.assessment.ship

    def to_json(self) -> dict[str, Any]:
        """Return the pipeline document: reviewers, judge output, score, ship flag and cost."""
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
        }

    def text(self) -> str:
        """Return a short plain-text report of the run, for use without `--json`."""
        output = self.judgement.model_dump(mode="json")
        lines = [f"pipeline run {self.pipeline_run_id}: {self.base}..{self.head}"]
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


def resolve_panel(
    reviewers: Sequence[str] | None = None,
    *,
    allow_unpublished: bool = False,
    trace_path: Path | None = None,
) -> Panel:
    """Resolve the judge and the selected reviewers, before anything runs.

    `reviewers` names agents of `REVIEWERS`; the default is all three. Unless `allow_unpublished`
    is set, only published versions resolve. Raises `UsageError` for an empty or unknown name and
    the resolver's `NotFoundError` subclasses (exit 3) when an agent or version is missing.
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
    return Panel(reviewers=specs, judge=judge)


def _resolve(
    name: str, version_range: str, allow_unpublished: bool, trace_path: Path | None
) -> LLMSpec:
    spec = resolve(name, version_range, published_only=not allow_unpublished, trace_path=trace_path)
    if not isinstance(spec, LLMSpec):
        raise UsageError(f"{spec.address} is not an LLM agent, so the panel cannot run it")
    return spec


def run_panel(
    panel: Panel,
    repo_path: str | Path,
    base: str,
    head: str,
    trace: Trace,
    *,
    runner: Runner = run,
    pipeline_run_id: str | None = None,
) -> PanelResult:
    """Review the change from `base` to `head` of the repository at `repo_path`.

    The reviewers run in a thread pool, and each `runner` call gets the same `pipeline_run_id`, as
    does the judge's, so the trace holds one row per agent under that id. A reviewer that raises is
    reported in the judge's input as failed, and the panel goes on. Raises `ToolNotFoundError` for
    an unknown repository or ref, `FactoryError` (exit 1) when every reviewer failed, and whatever
    the judge's run raises.
    """
    start = time.monotonic()
    run_id = pipeline_run_id or new_pipeline_run_id()
    diff = show_diff(repo_path, base, head, max_bytes=MAX_OUTPUT_BYTES)
    triage(diff)
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
    )


def command(
    repo_path: str,
    *,
    base: str,
    head: str,
    as_json: bool,
    reviewers: Sequence[str] | None = None,
    allow_unpublished: bool = False,
) -> int:
    """Run `factory review`: review `base..head` of `repo_path` with the panel and report it.

    Prose goes to stderr under `as_json`, where stdout gets exactly one JSON document, also when
    the change does not ship. Without it the text report goes to stdout. Returns 0 when the change
    ships and 1 when it does not.
    """
    panel = resolve_panel(reviewers, allow_unpublished=allow_unpublished)
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(
            f"{API_KEY_ENV} is not set",
            hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
        )
    with Trace() as trace:
        result = run_panel(panel, repo_path, base, head, trace, runner=run)
    if as_json:
        print(result.text(), file=sys.stderr)
        print(json.dumps(result.to_json()))
    else:
        print(result.text())
    return ExitCode.OK if result.ship else ExitCode.ERROR
