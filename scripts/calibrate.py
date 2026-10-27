"""Calibrate the diff-triage skip threshold against the labelled diffs.

Usage: `uv run python scripts/calibrate.py [options]`, run from the repository root. It fetches
every diff in `evals/calibration/labels.yaml` from GitHub by its commit pair, runs diff-triage over
each one once through the review panel's `run_triage`, and then sweeps the skip threshold
(`evals/calibration/sweep.py`). Per threshold it reports recall on risky diffs, the skip rate on
safe diffs and the cost per diff, as a table, or with `--json` as one JSON document on stdout.

Each diff's Jev call is traced under the pipeline run id `calibrate-<run>-<label id>`, and its cost
is read back by that id: the `triage:decision` row costs nothing and the Jev run's row carries the
real cost (docs/CONVENTIONS.md). The calls share one `Budget` (`--budget-usd`).

`--dry-run` fetches the diffs and reports only their sizes and how many exceed the manifest's
`max_chars`, with no Jev call. Nothing prints a diff's contents: they are untrusted data.

Exit codes follow `factory`: 0 a threshold reaches the manifest's `min_recall_risky`, 1 none does
or the sweep is incomplete because a Jev call failed, 2 bad arguments, 3 no labels file, 4 a
missing or rejected key, or `gh` cannot read a repository. docs/jev-calibration.md describes the
method and how to write the chosen threshold into the manifest.
"""

import argparse
import datetime
import json
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evals.calibration.fetch import fetch_all, fetch_diff
from evals.calibration.labels import LABELS_FILE, LabelledDiff, load_labels
from evals.calibration.sweep import (
    DEFAULT_THRESHOLDS,
    Sweep,
    Triaged,
    check_thresholds,
    render_table,
    sweep,
)
from factory.exit import (
    AuthOrConfigError,
    ExitCode,
    FactoryError,
    UsageError,
    error_envelope,
    exit_code,
)
from pipelines.review_panel import changed_files, run_triage
from registry import JevSpec, load_spec
from runtime.budget import Budget
from runtime.jev_runner import run_jev
from runtime.trace import Trace

MANIFEST = Path("agents/diff-triage/agent.yaml")
DEFAULT_BUDGET_USD = 1.0
"""Enough for the labelled set at jev-1.14.0 prices with a wide margin; the run stops past it."""
RUN_PREFIX = "calibrate-"
"""Every pipeline run id this script writes starts with this, so shadow reports can leave it out."""

type Fetch = Callable[[str, str, str], str]


MAX_ERROR_CHARS = 200
"""How much of a failed call's error the script prints: its first line, cut to this length."""


class SweepIncomplete(FactoryError):
    """At least one Jev call failed, so the sweep left those diffs out (exit 1)."""


class NoThreshold(FactoryError):
    """No swept threshold reaches the manifest's recall on risky diffs (exit 1)."""


class _Capturing:
    """A Jev runner that keeps the exception it raised, which `run_triage` reports only as text."""

    def __init__(self, runner: Callable[..., Any]) -> None:
        self.runner = runner
        self.error: Exception | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.runner(*args, **kwargs)
        except Exception as exc:
            self.error = exc
            raise


def triage_all(
    spec: JevSpec,
    labels: Sequence[LabelledDiff],
    texts: dict[str, str],
    trace: Trace,
    *,
    budget: Budget,
    jev_runner: Callable[..., Any] = run_jev,
    run: str | None = None,
) -> list[Triaged]:
    """Run diff-triage over every labelled diff once and return what each one answered.

    A failed call is kept as a `Triaged` without output, so the sweep lists it. A missing or
    rejected key would fail every call the same way, so the first auth failure is raised at once
    as an `AuthOrConfigError`. `BudgetExceeded` propagates from `run_triage`.
    """
    run = run or uuid.uuid4().hex[:8]
    triaged: list[Triaged] = []
    for label in labels:
        capturing = _Capturing(jev_runner)
        pipeline_run_id = f"{RUN_PREFIX}{run}-{label.id}"
        result = run_triage(
            spec, texts[label.id], trace, pipeline_run_id, jev_runner=capturing, budget=budget
        )
        error = capturing.error
        if error is not None and exit_code(error) is ExitCode.AUTH_OR_CONFIG:
            if isinstance(error, FactoryError):
                raise error
            raise AuthOrConfigError(
                f"{label.id}: {_first_line(error)}",
                hint="check ANTHROPIC_API_KEY and TYPESAFE_API_KEY",
            ) from error
        output = None
        if result.status == "ok" and result.probabilities is not None:
            output = spec.output_class.model_validate(result.probabilities)
        triaged.append(
            Triaged(
                id=label.id,
                risky=label.risky,
                output=output,
                truncated=result.truncated,
                cost_usd=trace.cost_by_pipeline_run(pipeline_run_id),
            )
        )
        if output is None:
            reason = "no output" if error is None else _first_line(error)
            print(f"calibrate: {label.id}: triage failed ({reason})", file=sys.stderr)
    return triaged


def sizes(spec: JevSpec, labels: Sequence[LabelledDiff], texts: dict[str, str]) -> dict[str, Any]:
    """Return the dry-run report: each diff's rendered length and whether it exceeds `max_chars`."""
    limit = spec.input.max_chars
    rows: list[dict[str, Any]] = []
    over: list[LabelledDiff] = []
    for label in labels:
        diff = texts[label.id]
        chars = len(spec.input.render.format(files="\n".join(changed_files(diff)), diff=diff))
        if chars > limit:
            over.append(label)
        rows.append(
            {"id": label.id, "risky": label.risky, "chars": chars, "truncated": chars > limit}
        )
    return {
        "max_chars": limit,
        "diffs": len(labels),
        "truncated": len(over),
        "safe": sum(1 for label in labels if not label.risky),
        "safe_truncated": sum(1 for label in over if not label.risky),
        "rows": rows,
    }


def manifest_snippet(result: Sweep, today: datetime.date) -> str | None:
    """Return the skip gate to paste into the manifest for the chosen threshold, or `None`.

    The comment gives the date and the dataset size, as the manifest's thresholds must.
    """
    chosen = result.chosen
    if chosen is None:
        return None
    question, _, option = result.question.partition("=")
    lines = [
        (
            f"  # Calibrated {today.isoformat()} on {result.diffs} labelled diffs "
            f"({chosen.risky} risky): recall {chosen.recall_risky:.3f}, "
            f"safe skip rate {chosen.skip_rate_safe:.3f} (scripts/calibrate.py)."
        ),
        "  - name: low_risk",
        f"    question: {question}",
        "    rule: p_below",
        f"    value: {chosen.threshold}",
        "    action: skip",
    ]
    if option:
        lines.insert(3, f"    option: {option}")
    return "\n".join(lines)


def _thresholds(text: str) -> tuple[float, ...]:
    """Parse and check `--thresholds` before anything is fetched or a Jev call is paid for."""
    try:
        values = [float(part) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise UsageError(f"--thresholds: {exc}", hint="pass numbers such as 0.05,0.1,0.2") from exc
    return check_thresholds(values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/calibrate.py",
        description="Fetch the labelled diffs, run diff-triage over each, and sweep the skip "
        "threshold. Calls the live Jev API unless --dry-run is given.",
        epilog="example: uv run python scripts/calibrate.py --panel-cost-usd 0.35 --json",
    )
    parser.add_argument("--labels", type=Path, default=LABELS_FILE, help="the labels file")
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST, help="the diff-triage manifest to calibrate"
    )
    parser.add_argument(
        "--thresholds",
        default=",".join(map(str, DEFAULT_THRESHOLDS)),
        help="comma-separated skip thresholds in (0, 1]",
    )
    parser.add_argument(
        "--question",
        help="the question the skip gate reads, as ID or ID=OPTION; "
        "default: the manifest's skip gate, else needs_review",
    )
    parser.add_argument(
        "--budget-usd", type=float, default=DEFAULT_BUDGET_USD, help="cap on the Jev spend"
    )
    parser.add_argument(
        "--panel-cost-usd",
        type=float,
        help="cost of one full panel review, added to the cost per diff for each diff reviewed",
    )
    parser.add_argument("--trace", type=Path, help="trace database (default: FACTORY_TRACE_DB)")
    parser.add_argument("--limit", type=int, help="use only the first N labelled diffs")
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch the diffs and report their sizes only"
    )
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetch = fetch_diff,
    jev_runner: Callable[..., Any] = run_jev,
    today: datetime.date | None = None,
) -> int:
    """Run the calibration and return the exit code; `fetch` and `jev_runner` are for tests."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _calibrate(args, fetch, jev_runner, today or datetime.date.today())
    except FactoryError as exc:
        if args.json:
            print(json.dumps(error_envelope(exc)), file=sys.stderr)
        else:
            hint = f" ({exc.hint})" if exc.hint else ""
            print(f"calibrate: {exc.message}{hint}", file=sys.stderr)
        return exit_code(exc)


def _calibrate(
    args: argparse.Namespace,
    fetch: Fetch,
    jev_runner: Callable[..., Any],
    today: datetime.date,
) -> int:
    thresholds = _thresholds(args.thresholds)
    if args.limit is not None and args.limit < 1:
        raise UsageError("--limit must be at least 1")
    question, _, option = (args.question or "").partition("=")
    spec = load_spec(args.manifest)
    if not isinstance(spec, JevSpec):
        raise UsageError(f"{args.manifest} is not a Jev manifest", hint="pass --manifest")
    labels = load_labels(args.labels)[: args.limit]
    print(f"calibrate: fetching {len(labels)} diffs", file=sys.stderr)
    texts = fetch_all(labels, fetch=fetch)

    if args.dry_run:
        report = sizes(spec, labels, texts)
        if args.json:
            print(json.dumps(report))
        else:
            print(
                f"{report['diffs']} diffs fetched; {report['truncated']} exceed max_chars "
                f"{report['max_chars']} and are always reviewed, {report['safe_truncated']} of "
                f"them among {report['safe']} safe diffs"
            )
        return 0

    with Trace(args.trace) as trace:
        triaged = triage_all(
            spec, labels, texts, trace, budget=Budget(args.budget_usd), jev_runner=jev_runner
        )
    if not question and not _has_skip_gate(spec):
        question = "needs_review"
    result = sweep(
        spec,
        triaged,
        thresholds,
        question=question or None,
        option=option or None,
        panel_cost_usd=args.panel_cost_usd,
    )
    snippet = manifest_snippet(result, today)
    if args.json:
        print(json.dumps({**result.to_json(), "manifest_snippet": snippet}))
    else:
        print(render_table(result))
        if snippet is not None:
            print(f"\nskip gate for {args.manifest} (bump its version):\n{snippet}")
    if result.failed:
        raise SweepIncomplete(
            f"sweep incomplete: {len(result.failed)} of {len(triaged)} triage calls failed",
            hint="the failed diffs are listed above; fix the cause and run again",
        )
    if result.chosen is None:
        raise NoThreshold(
            f"no threshold reaches recall {result.min_recall_risky} on risky diffs",
            hint="the table shows the risky diffs each threshold misses; keep the gate off",
        )
    return 0


def _has_skip_gate(spec: JevSpec) -> bool:
    return any(gate.action == "skip" for gate in spec.gates)


def _first_line(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip().splitlines()[0]
    return text[:MAX_ERROR_CHARS]


if __name__ == "__main__":
    sys.exit(main())
