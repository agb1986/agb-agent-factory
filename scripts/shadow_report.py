"""Report what the diff-triage skip gate would have skipped, from the review panel's trace rows.

Usage: `uv run python scripts/shadow_report.py [options]`, run from the repository root. While
`runtime.gates.ROUTE_ONLY` is on, the review panel triages every diff, logs the `Decision` in a
`triage:decision` trace row and still runs every reviewer. This script reads those rows for a range
of UTC days, both ends included, and decides each diff again at the skip threshold: the manifest's
skip gate by default, or `--threshold`. For every diff that would have been skipped it reports the
most severe finding the panel's reviewers and judge made under the same pipeline run id, and what
the run cost. A would-skip diff with a blocker finding means the threshold is too high
(docs/jev-calibration.md, "Shadow mode").

Runs of `scripts/calibrate.py`, whose pipeline run ids start with `calibrate-`, are left out. A
truncated input or a failed Jev call never skips, as in `runtime.gates.decide`. A row whose answers
do not fit the manifest's questions, such as one written by another version of the agent, is
counted as unreadable and decided no further. The cost of a run is the sum of `cost_usd` over its
pipeline run id; the triage decision row costs nothing, and the Jev run's row carries the real cost
(docs/CONVENTIONS.md). The store is opened read-only. Nothing prints a diff, a file name or a
finding's text: they are untrusted data.

Exit codes follow `factory`: 0 no would-skip diff has a finding at `--fail-on` severity or worse
(default blocker), 1 at least one does, or the store cannot be opened, 2 bad arguments, 3 no trace
store, 4 a bad manifest.
"""

import argparse
import datetime
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
from pydantic import ValidationError

from factory.exit import FactoryError, NotFoundError, UsageError, error_envelope, exit_code
from pipelines.review_panel import TRIAGE_TAG
from registry import JevSpec, load_spec
from registry.jev_spec import ProbabilityGate
from runtime.gates import decide, skip_gate, with_skip_threshold
from runtime.trace import resolve_path

MANIFEST = Path("agents/diff-triage/agent.yaml")
CALIBRATE_PREFIX = "calibrate-"
"""`RUN_PREFIX` of `scripts/calibrate.py`: runs whose pipeline run id starts so are left out."""
SHADOW_DAYS = 14
"""How many days the report covers when `--since` is not given, `--until` included."""
SEVERITIES = ("info", "minor", "major", "blocker")
"""Finding severities, least severe first."""
STATUSES = ("skip", "review", "truncated", "jev_error", "unreadable")
"""What became of a triaged diff at the threshold; only `skip` would have skipped the panel."""

_DECISIONS = """
    SELECT pipeline_run_id, agent_ref, timezone('UTC', started_at),
           decision_json, output_json, error
    FROM traces
    WHERE tag = ? AND starts_with(agent_ref, ?) AND started_at >= ? AND started_at < ?
      AND pipeline_run_id IS NOT NULL AND NOT starts_with(pipeline_run_id, ?)
    ORDER BY started_at, pipeline_run_id
"""
# Only the commit pair is read from an input: a Jev or judge input holds the diff itself.
_RUN_ROWS = """
    SELECT pipeline_run_id, agent_ref, kind, COALESCE(cost_usd, 0.0),
           json_extract_string(input_json, '$.base'), json_extract_string(input_json, '$.head'),
           CASE WHEN kind = 'llm' THEN output_json END
    FROM traces
    WHERE list_contains(?, pipeline_run_id)
"""


class ShadowFailed(FactoryError):
    """A diff the gate would have skipped got a finding at the `--fail-on` severity (exit 1)."""


@dataclass(frozen=True, slots=True)
class ShadowRun:
    """One triaged pipeline run, decided again at the threshold.

    `status` is one of `STATUSES`. `p` is the probability the skip gate reads, `None` when the
    answers are missing or unreadable. `severity` is the most severe finding any panel agent made,
    `None` when there was none, and `reviewed` says whether a reviewer or the judge wrote an output
    under the run at all. `cost_usd` is the whole run and `triage_cost_usd` the triage agent's part.
    """

    pipeline_run_id: str
    started_at: datetime.datetime
    agent_ref: str
    status: str
    p: float | None
    severity: str | None
    reviewed: bool
    base: str | None
    head: str | None
    cost_usd: float
    triage_cost_usd: float

    def at_least(self, severity: str) -> bool:
        """Whether the run has a finding at `severity` or worse."""
        return self.severity is not None and _rank(self.severity) >= _rank(severity)

    def to_json(self) -> dict[str, Any]:
        """Return the run as a JSON-ready dict."""
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "started_at": self.started_at.isoformat(),
            "agent_ref": self.agent_ref,
            "status": self.status,
            "p": self.p,
            "severity": self.severity,
            "reviewed": self.reviewed,
            "base": self.base,
            "head": self.head,
            "cost_usd": self.cost_usd,
            "triage_cost_usd": self.triage_cost_usd,
        }


@dataclass(frozen=True, slots=True)
class Report:
    """Every triaged run in the range at one threshold, and what skipping them would have meant.

    `failing` lists the would-skip runs with a finding at `fail_on` or worse, and `unchecked` the
    would-skip runs no panel agent wrote an output for, so their severity is unknown.
    """

    since: datetime.date
    until: datetime.date
    agent: str
    question: str
    threshold: float
    fail_on: str
    runs: tuple[ShadowRun, ...] = field(default=())

    @property
    def would_skip(self) -> tuple[ShadowRun, ...]:
        """The runs the gate would have skipped."""
        return tuple(run for run in self.runs if run.status == "skip")

    @property
    def failing(self) -> tuple[ShadowRun, ...]:
        """The would-skip runs with a finding at `fail_on` or worse."""
        return tuple(run for run in self.would_skip if run.at_least(self.fail_on))

    @property
    def unchecked(self) -> tuple[ShadowRun, ...]:
        """The would-skip runs without any panel output."""
        return tuple(run for run in self.would_skip if not run.reviewed)

    def counts(self) -> dict[str, int]:
        """Return how many runs ended in each status, every status listed."""
        return {status: sum(run.status == status for run in self.runs) for status in STATUSES}

    def panel_cost_per_run_usd(self) -> float | None:
        """Return the mean cost of a run less its triage, over runs with panel output, or `None`."""
        reviewed = [run for run in self.runs if run.reviewed]
        if not reviewed:
            return None
        return sum(run.cost_usd - run.triage_cost_usd for run in reviewed) / len(reviewed)

    def saved_usd(self) -> float:
        """Return what skipping would have saved: each would-skip run's cost less its triage."""
        return sum(run.cost_usd - run.triage_cost_usd for run in self.would_skip)

    def to_json(self) -> dict[str, Any]:
        """Return the report as a JSON-ready dict; `would_skip` holds the full runs."""
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "agent": self.agent,
            "question": self.question,
            "threshold": self.threshold,
            "fail_on": self.fail_on,
            "runs": len(self.runs),
            "counts": self.counts(),
            "skip_rate": len(self.would_skip) / len(self.runs) if self.runs else None,
            "panel_runs": sum(run.reviewed for run in self.runs),
            "panel_cost_per_run_usd": self.panel_cost_per_run_usd(),
            "saved_usd": self.saved_usd(),
            "would_skip": [run.to_json() for run in self.would_skip],
            "failing": [run.pipeline_run_id for run in self.failing],
            "unchecked": [run.pipeline_run_id for run in self.unchecked],
        }


def shadow_runs(
    con: duckdb.DuckDBPyConnection,
    spec: JevSpec,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[ShadowRun]:
    """Read the triage decisions started in `[start, end)` and decide each again with `spec`.

    Only the decisions of `spec`'s agent, at any version, are read. `spec` carries the skip gate
    at the threshold being reported. Every row under a decision's pipeline run id is joined in for
    the cost, the commit pair and the panel's findings.
    """
    gate = skip_gate(spec)
    assert gate is not None  # the caller always passes a spec from with_skip_threshold
    params = [TRIAGE_TAG, f"{spec.name}@", start, end, CALIBRATE_PREFIX]
    decisions = con.execute(_DECISIONS, params).fetchall()
    ids = [row[0] for row in decisions]
    per_run: dict[str, list[tuple[Any, ...]]] = {run_id: [] for run_id in ids}
    if ids:
        for row in con.execute(_RUN_ROWS, [ids]).fetchall():
            per_run[row[0]].append(row[1:])
    runs = []
    for run_id, agent_ref, started, decision_json, output_json, error in decisions:
        status, p = _decide(spec, gate, _loads(decision_json), _loads(output_json), error)
        rows = per_run[run_id]
        outputs = [_loads(output) for _, kind, _, _, _, output in rows if kind == "llm"]
        found = [severity for output in outputs for severity in _severities(output)]
        base = next((base for _, _, _, base, _, _ in rows if base is not None), None)
        head = next((head for _, _, _, _, head, _ in rows if head is not None), None)
        runs.append(
            ShadowRun(
                pipeline_run_id=run_id,
                started_at=started.replace(tzinfo=datetime.UTC),
                agent_ref=agent_ref,
                status=status,
                p=p,
                severity=max(found, key=_rank, default=None),
                reviewed=any(output is not None for output in outputs),
                base=base,
                head=head,
                cost_usd=sum(cost for _, _, cost, _, _, _ in rows),
                triage_cost_usd=sum(
                    cost for ref, kind, cost, _, _, _ in rows if kind == "jev" and ref == agent_ref
                ),
            )
        )
    return runs


def _decide(
    spec: JevSpec,
    gate: ProbabilityGate,
    decision: Any,
    output: Any,
    error: str | None,
) -> tuple[str, float | None]:
    """Return the status of one decision row at `spec`'s skip gate and the probability it read."""
    if error is not None or output is None:
        return "jev_error", None
    try:
        answers = spec.output_class.model_validate(output)
    except ValidationError:
        return "unreadable", None
    answer = getattr(answers, gate.question)
    p = answer.p if gate.option is None else answer.probs[gate.option]
    if isinstance(decision, dict) and decision.get("truncated"):
        return "truncated", p
    return ("skip" if decide(spec, answers, truncated=False).skip else "review"), p


def _severities(output: Any) -> list[str]:
    """The severity of every finding in a reviewer's (`findings`) or judge's output."""
    if not isinstance(output, dict):
        return []
    return [
        finding["severity"]
        for key in ("findings", "merged_findings")
        for finding in output.get(key) or ()
        if isinstance(finding, dict) and finding.get("severity") in SEVERITIES
    ]


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def render(report: Report) -> str:
    """Return the report as plain text: the counts, the cost, and one line per would-skip diff."""
    runs = len(report.runs)
    counts = report.counts()
    skipped = report.would_skip
    rate = f" ({len(skipped) / runs:.1%})" if runs else ""
    panel_cost = report.panel_cost_per_run_usd()
    lines = [
        (
            f"shadow report {report.since.isoformat()}..{report.until.isoformat()} (UTC): "
            f"{report.agent}, skip gate {report.question} p_below {report.threshold}"
        ),
        (
            f"{runs} triaged runs: {len(skipped)} would skip{rate}, {counts['review']} reviewed, "
            f"{counts['truncated']} truncated, {counts['jev_error']} jev_error, "
            f"{counts['unreadable']} unreadable"
        ),
        (
            "no panel output in range"
            if panel_cost is None
            else (
                f"panel cost per run ${panel_cost:.4f} over "
                f"{sum(run.reviewed for run in report.runs)} runs; "
                f"skipping would have saved ${report.saved_usd():.4f}"
            )
        ),
    ]
    if skipped:
        lines += ["", f"  {'started (UTC)':<16}  {'p':>6}  {'severity':<9}  {'cost':>9}  run"]
        for run in skipped:
            severity = run.severity or ("none" if run.reviewed else "no panel")
            lines.append(
                f"  {run.started_at:%Y-%m-%d %H:%M}  {run.p:>6.3f}  {severity:<9}  "
                f"{f'${run.cost_usd:.4f}':>9}  {run.pipeline_run_id}"
            )
    if report.unchecked:
        lines.append(f"\n{len(report.unchecked)} would-skip runs have no panel output to check")
    failing = report.failing
    if failing:
        ids = ", ".join(run.pipeline_run_id for run in failing)
        lines.append(f"\nFAIL: would-skip runs with a {report.fail_on} finding or worse: {ids}")
    else:
        lines.append(f"\nOK: no would-skip run has a {report.fail_on} finding or worse")
    return "\n".join(lines)


def _date(flag: str, text: str | None, default: datetime.date) -> datetime.date:
    if text is None:
        return default
    try:
        return datetime.date.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(f"{flag}: {exc}", hint="pass a date such as 2026-10-27") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/shadow_report.py",
        description="Report the diffs the diff-triage skip gate would have skipped, read from the "
        "trace store, and the most severe panel finding on each.",
        epilog="example: uv run python scripts/shadow_report.py --since 2026-11-02 --json",
    )
    parser.add_argument(
        "--since",
        help=f"first UTC day, YYYY-MM-DD (default: the {SHADOW_DAYS} days ending at --until)",
    )
    parser.add_argument("--until", help="last UTC day, YYYY-MM-DD, included (default: today)")
    parser.add_argument(
        "--threshold", type=float, help="skip threshold (default: the manifest's skip gate)"
    )
    parser.add_argument(
        "--question",
        help="the question the skip gate reads, as ID or ID=OPTION; "
        "default: the manifest's skip gate, else needs_review",
    )
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST, help="the diff-triage manifest to report on"
    )
    parser.add_argument(
        "--fail-on",
        choices=("major", "blocker"),
        default="blocker",
        help="exit 1 when a would-skip diff has a finding this severe or worse",
    )
    parser.add_argument("--trace", type=Path, help="trace database (default: FACTORY_TRACE_DB)")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def main(argv: Sequence[str] | None = None, *, today: datetime.date | None = None) -> int:
    """Print the shadow report and return the exit code; `today` is for tests."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _report(args, today or datetime.datetime.now(datetime.UTC).date())
    except FactoryError as exc:
        if args.json:
            print(json.dumps(error_envelope(exc)), file=sys.stderr)
        else:
            hint = f" ({exc.hint})" if exc.hint else ""
            print(f"shadow_report: {exc.message}{hint}", file=sys.stderr)
        return exit_code(exc)


def _report(args: argparse.Namespace, today: datetime.date) -> int:
    until = _date("--until", args.until, today)
    since = _date("--since", args.since, until - datetime.timedelta(days=SHADOW_DAYS - 1))
    if since > until:
        raise UsageError(f"--since {since} is after --until {until}")
    spec = load_spec(args.manifest)
    if not isinstance(spec, JevSpec):
        raise UsageError(f"{args.manifest} is not a Jev manifest", hint="pass --manifest")
    question, _, option = (args.question or "").partition("=")
    gated = _gated(spec, args.threshold, question or None, option or None)
    gate = skip_gate(gated)
    assert gate is not None  # _gated always leaves one

    path = resolve_path(args.trace)
    if not path.exists():
        raise NotFoundError(
            f"no trace store at {path}", hint="pass --trace or set FACTORY_TRACE_DB"
        )
    start = datetime.datetime.combine(since, datetime.time(), datetime.UTC)
    end = datetime.datetime.combine(
        until + datetime.timedelta(days=1), datetime.time(), datetime.UTC
    )
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        raise FactoryError(
            f"could not open {path}: {str(exc).splitlines()[0]}",
            hint="a process writing to the store holds it; run again when it is done",
        ) from exc
    try:
        con.execute("SET TimeZone = 'UTC'")
        runs = shadow_runs(con, gated, start, end)
    except duckdb.CatalogException as exc:
        raise NotFoundError(f"{path} has no traces table", hint="pass --trace") from exc
    finally:
        con.close()

    report = Report(
        since=since,
        until=until,
        agent=spec.address,
        question=gate.question if gate.option is None else f"{gate.question}={gate.option}",
        threshold=gate.value,
        fail_on=args.fail_on,
        runs=tuple(runs),
    )
    print(json.dumps(report.to_json()) if args.json else render(report))
    if report.failing:
        raise ShadowFailed(
            f"{len(report.failing)} would-skip runs have a {args.fail_on} finding or worse",
            hint="raise the threshold and restart the shadow clock (docs/jev-calibration.md)",
        )
    return 0


def _gated(
    spec: JevSpec, threshold: float | None, question: str | None, option: str | None
) -> JevSpec:
    """Return `spec` with its skip gate at `threshold`, or as the manifest has it when not given."""
    current = skip_gate(spec)
    if threshold is None:
        if current is None:
            raise UsageError(
                f"{spec.address} has no skip gate", hint="pass --threshold, such as 0.05"
            )
        threshold = current.value
    if not 0.0 < threshold <= 1.0:
        raise UsageError(f"--threshold must be in (0, 1], not {threshold}")
    if question is None and current is None:
        question = "needs_review"
    try:
        return with_skip_threshold(spec, threshold, question=question, option=option)
    except ValueError as exc:
        raise UsageError(str(exc), hint="pass --question needs_review") from exc


if __name__ == "__main__":
    sys.exit(main())
