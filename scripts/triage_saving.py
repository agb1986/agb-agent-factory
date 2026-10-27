"""Measure what diff-triage would save: the review panel over every fixture, with and without it.

Usage: `uv run python scripts/triage_saving.py [options]`, run from the repository root with
`ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY` set. The script builds the fixture repositories under
`fixtures/repos/` (cheap and deterministic, so it always does) and runs the review panel in-process
over each fixture's `base..head` twice: once with triage and once without, as `factory review
--no-triage` would. Both runs get the same reviewers, the same judge and a fresh budget of
`--budget-usd`. The judge gate is off in both, so the triage agent is the only Jev call and the one
difference between the two runs. The pipeline run ids are `triage-saving-<run>-<fixture>-triage`
and `triage-saving-<run>-<fixture>-no-triage`, where `<run>` is a fresh token per invocation. The
with-triage runs write `triage:decision` rows that `scripts/shadow_report.py` reads too, so pass
`--trace-db` with a separate store to keep them out of the shadow report.

While `runtime.gates.ROUTE_ONLY` holds, the with-triage run still runs every reviewer, so the two
runs cost about the same and the saving is projected. A fixture whose `Decision` skips at the
manifest's skip gate would have cost only its triage, so the projected saving is the reviewers'
and the judge's cost in the with-triage runs of the would-skip fixtures, less the triage agent's
cost over every fixture. Every number is read back from the trace rows after the runs: the cost of
a run is the sum of `cost_usd` over its pipeline run id, and Jev's cost is the part in `jev` rows
(docs/CONVENTIONS.md).

A would-skip fixture with a `major` finding or worse, in either run, means the gate would have
skipped a diff that needed review, and the script exits 1. So it does when a run fails, when the
Jev call fails, or when spending stops it: no further fixture starts once the spend so far, or
after three fixtures the spend projected over all of them, passes `--max-usd`, so a run can go
past `--max-usd` by at most one fixture's cost. What was measured
is still reported. `--dry-run` lists the fixtures and agents and calls nothing.

Nothing prints a diff, a file name from a fixture or a finding's text: they are untrusted data. A
failed run is reported by its exception type only.

Exit codes follow `factory`: 0 no would-skip fixture has a major finding or worse and every run
went through, 1 otherwise, 2 bad arguments, 3 an unknown fixture, agent or version, 4 a missing
key.
"""

import argparse
import dataclasses
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from factory.exit import (
    AuthOrConfigError,
    ExitCode,
    FactoryError,
    NotFoundError,
    UsageError,
    error_envelope,
    exit_code,
)
from fixtures.build import BASE_REF, HEAD_REF, REPOS_DIR, BuiltFixture, FixtureNotFound, discover
from fixtures.build import build as build_fixtures
from pipelines.review_panel import TRIAGE_TAG, Panel, resolve_panel, run_panel
from runtime.budget import DEFAULT_BUDGET_USD, Budget
from runtime.gates import skip_gate
from runtime.jev_runner import API_KEY_ENV as JEV_KEY_ENV
from runtime.jev_runner import run_jev
from runtime.runner import API_KEY_ENV as CLAUDE_KEY_ENV
from runtime.runner import run
from runtime.trace import Trace, resolve_path

RUN_PREFIX = "triage-saving-"
"""What every pipeline run id of the script starts with."""
MODES = ("triage", "no-triage")
"""The two runs per fixture, in the order they run; each is the last part of its run id."""
SEVERITIES = ("info", "minor", "major", "blocker")
"""Finding severities, least severe first."""
FAIL_ON = "major"
"""A would-skip fixture with a finding this severe or worse fails the measurement."""
MAX_USD = 5.0
"""Default `--max-usd`: what one invocation may spend in all."""
PROJECT_AFTER = 3
"""After this many fixtures the spend so far is projected over all of them and checked too."""

REPOS = REPOS_DIR
"""Where the fixtures are built; a module attribute so tests can point it elsewhere."""

# Only an LLM output is read, for its findings' severities: an input holds the diff itself.
_ROWS = """
    SELECT pipeline_run_id, kind, tag, COALESCE(cost_usd, 0.0), decision_json, error,
           CASE WHEN kind = 'llm' THEN output_json END
    FROM traces
    WHERE starts_with(pipeline_run_id, ?)
"""


class SavingFailed(FactoryError):
    """A would-skip fixture had a major finding or worse, a run failed, or spend stopped it (exit 1)."""


def pipeline_run_id(token: str, fixture: str, mode: str) -> str:
    """Return the pipeline run id of one run: `triage-saving-<token>-<fixture>-<mode>`."""
    return f"{RUN_PREFIX}{token}-{fixture}-{mode}"


@dataclass(frozen=True, slots=True)
class Measurement:
    """What running the panel did, before anything is read back.

    `fixtures` are the names measured, in order, `errors` maps a pipeline run id to the exception
    type that ended it, and `stopped` says why spending stopped the measurement before its last
    fixture, `None` when it did not.
    """

    token: str
    fixtures: tuple[str, ...]
    errors: dict[str, str]
    stopped: str | None


def measure(
    panel: Panel,
    built: Sequence[BuiltFixture],
    trace: Trace,
    *,
    token: str,
    budget_usd: float = DEFAULT_BUDGET_USD,
    max_usd: float = MAX_USD,
    runner: Callable[..., Any] = run,
    jev_runner: Callable[..., Any] = run_jev,
) -> Measurement:
    """Run `panel` over each fixture with triage, then again without it, and return what happened.

    Each run gets its own `Budget(budget_usd)`. A run that raises is recorded by exception type
    and the rest still run, except for a missing or rejected key, which would fail every run the
    same way and is raised at once. Before each fixture the spend so far is checked against
    `max_usd` (see the module docstring); passing it stops the loop, so the last fixture can take
    the spend past `max_usd` by at most what one fixture costs.
    """
    plain = dataclasses.replace(panel, triage=None)
    done: list[str] = []
    errors: dict[str, str] = {}
    spent = 0.0
    for fixture in built:
        stopped = _over(spent, len(done), len(built), max_usd)
        if stopped is not None:
            return Measurement(token, tuple(done), errors, stopped)
        costs = []
        for mode, which in zip(MODES, (panel, plain), strict=True):
            run_id = pipeline_run_id(token, fixture.name, mode)
            try:
                run_panel(
                    which,
                    fixture.path,
                    BASE_REF,
                    HEAD_REF,
                    trace,
                    runner=runner,
                    jev_runner=jev_runner,
                    pipeline_run_id=run_id,
                    budget=Budget(budget_usd),
                )
            except Exception as exc:  # a failed run is reported; the rest still run
                if exit_code(exc) is ExitCode.AUTH_OR_CONFIG:
                    raise
                errors[run_id] = type(exc).__name__
            costs.append(trace.cost_by_pipeline_run(run_id))
        spent += sum(costs)
        done.append(fixture.name)
        print(
            f"triage_saving: {fixture.name}: ${costs[0]:.4f} with triage, "
            f"${costs[1]:.4f} without; ${spent:.4f} so far",
            file=sys.stderr,
        )
    return Measurement(token, tuple(done), errors, None)


def _over(spent: float, done: int, total: int, max_usd: float) -> str | None:
    """Say why spending must stop, or return `None` while it may go on."""
    if spent > max_usd:
        return f"spent ${spent:.4f}, over --max-usd ${max_usd:.2f}"
    if PROJECT_AFTER <= done < total and spent / done * total > max_usd:
        return (
            f"${spent:.4f} over {done} fixtures projects to ${spent / done * total:.4f} "
            f"over {total}, over --max-usd ${max_usd:.2f}"
        )
    return None


@dataclass(frozen=True, slots=True)
class RunCost:
    """One panel run as the trace has it: its cost, the triage agent's part, and its failure."""

    pipeline_run_id: str
    cost_usd: float
    jev_cost_usd: float
    error: str | None

    def to_json(self) -> dict[str, Any]:
        """Return the run as a JSON-ready dict; the Jev part is on the fixture."""
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "cost_usd": _round(self.cost_usd),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class FixtureSaving:
    """Both runs of one fixture, the triage `Decision` and the most severe finding of either.

    `decision` holds `status` (`ok` or `jev_error`), `skip`, `escalate`, `route` and `reason`, and
    is `None` when the with-triage run ended before triage wrote its row.
    """

    fixture: str
    with_triage: RunCost
    no_triage: RunCost
    decision: dict[str, Any] | None
    severity: str | None

    @property
    def would_skip(self) -> bool:
        """Whether the manifest's skip gate would have skipped the panel."""
        return self.decision is not None and self.decision["skip"] is True

    @property
    def jev_cost_usd(self) -> float:
        """What the triage agent cost."""
        return self.with_triage.jev_cost_usd

    @property
    def panel_cost_usd(self) -> float:
        """What the reviewers and the judge cost in the with-triage run: what a skip saves."""
        return self.with_triage.cost_usd - self.with_triage.jev_cost_usd

    @property
    def failed(self) -> bool:
        """Whether either run failed, so the fixture's numbers are incomplete."""
        return self.with_triage.error is not None or self.no_triage.error is not None

    def at_least(self, severity: str) -> bool:
        """Whether either run has a finding at `severity` or worse."""
        return self.severity is not None and _rank(self.severity) >= _rank(severity)

    def to_json(self) -> dict[str, Any]:
        """Return the fixture as a JSON-ready dict."""
        return {
            "fixture": self.fixture,
            "with_triage": self.with_triage.to_json(),
            "no_triage": self.no_triage.to_json(),
            "jev_cost_usd": _round(self.jev_cost_usd),
            "decision": self.decision,
            "severity": self.severity,
        }


def read_back(con: duckdb.DuckDBPyConnection, measurement: Measurement) -> list[FixtureSaving]:
    """Read the rows of every run in `measurement` and return one `FixtureSaving` per fixture."""
    rows: dict[str, list[tuple[Any, ...]]] = {}
    prefix = f"{RUN_PREFIX}{measurement.token}-"
    for row in con.execute(_ROWS, [prefix]).fetchall():
        rows.setdefault(row[0], []).append(row[1:])
    savings = []
    for fixture in measurement.fixtures:
        runs: dict[str, RunCost] = {}
        decision: dict[str, Any] | None = None
        found: list[str] = []
        for mode in MODES:
            run_id = pipeline_run_id(measurement.token, fixture, mode)
            error = measurement.errors.get(run_id)
            for kind, tag, _, decision_json, row_error, output in rows.get(run_id, ()):
                found += _severities(_loads(output))
                if tag == TRIAGE_TAG and mode == "triage":
                    decision = _decision(_loads(decision_json), row_error)
                    if decision["status"] == "jev_error" and error is None:
                        error = "jev_error"
            run_rows = rows.get(run_id, ())
            runs[mode] = RunCost(
                pipeline_run_id=run_id,
                cost_usd=sum(cost for _, _, cost, _, _, _ in run_rows),
                jev_cost_usd=sum(cost for kind, _, cost, _, _, _ in run_rows if kind == "jev"),
                error=error,
            )
        savings.append(
            FixtureSaving(
                fixture=fixture,
                with_triage=runs["triage"],
                no_triage=runs["no-triage"],
                decision=decision,
                severity=max(found, key=_rank, default=None),
            )
        )
    return savings


def _decision(decision: Any, error: str | None) -> dict[str, Any]:
    """The parts of a `triage:decision` row's decision the report shows."""
    decision = decision if isinstance(decision, dict) else {}
    return {
        "status": "ok" if error is None else "jev_error",
        "skip": bool(decision.get("skip")),
        "escalate": bool(decision.get("escalate")),
        "route": list(decision.get("route") or ()),
        "reason": decision.get("reason"),
    }


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


def _round(cost: float) -> float:
    return round(cost, 6)


@dataclass(frozen=True, slots=True)
class Report:
    """One measurement: the agents, the skip gate, every fixture's runs and the totals.

    `question` and `threshold` are the manifest's skip gate, both `None` when it has none.
    """

    token: str
    reviewers: tuple[str, ...]
    judge: str
    triage: str
    question: str | None
    threshold: float | None
    budget_usd: float
    max_usd: float
    fixtures: tuple[FixtureSaving, ...]
    stopped: str | None = None

    @property
    def would_skip(self) -> tuple[FixtureSaving, ...]:
        """The fixtures the skip gate would have skipped."""
        return tuple(saving for saving in self.fixtures if saving.would_skip)

    @property
    def would_skip_major(self) -> tuple[FixtureSaving, ...]:
        """The would-skip fixtures with a `FAIL_ON` finding or worse."""
        return tuple(saving for saving in self.would_skip if saving.at_least(FAIL_ON))

    @property
    def failing(self) -> tuple[FixtureSaving, ...]:
        """The fixtures with a failed run."""
        return tuple(saving for saving in self.fixtures if saving.failed)

    def totals(self) -> dict[str, Any]:
        """Return both totals, Jev's overhead and the projected saving, in dollars, and counts."""
        with_triage = sum(saving.with_triage.cost_usd for saving in self.fixtures)
        no_triage = sum(saving.no_triage.cost_usd for saving in self.fixtures)
        jev = sum(saving.jev_cost_usd for saving in self.fixtures)
        skipped = sum(saving.panel_cost_usd for saving in self.would_skip)
        return {
            "fixtures": len(self.fixtures),
            "with_triage_usd": _round(with_triage),
            "no_triage_usd": _round(no_triage),
            "difference_usd": _round(with_triage - no_triage),
            "jev_overhead_usd": _round(jev),
            "would_skip": len(self.would_skip),
            "projected_saving_usd": _round(skipped - jev),
            "would_skip_major": len(self.would_skip_major),
            "measurement_usd": _round(with_triage + no_triage),
        }

    def to_json(self) -> dict[str, Any]:
        """Return the report as a JSON-ready dict."""
        return {
            "run": self.token,
            "agents": {
                "reviewers": list(self.reviewers),
                "judge": self.judge,
                "triage": self.triage,
            },
            "skip_gate": _gate_json(self.question, self.threshold),
            "budget_usd": self.budget_usd,
            "max_usd": self.max_usd,
            "fixtures": [saving.to_json() for saving in self.fixtures],
            "totals": self.totals(),
            "failing": [saving.fixture for saving in self.failing],
            "stopped": self.stopped,
        }


def _gate_json(question: str | None, threshold: float | None) -> dict[str, Any] | None:
    return None if question is None else {"question": question, "threshold": threshold}


def _gate_text(question: str | None, threshold: float | None) -> str:
    return "no skip gate" if question is None else f"skip gate {question} p_below {threshold}"


def _decision_text(decision: dict[str, Any] | None) -> str:
    if decision is None:
        return "none"
    if decision["status"] != "ok":
        return decision["status"]
    return "skip" if decision["skip"] else "escalate" if decision["escalate"] else "review"


def render(report: Report) -> str:
    """Return the report as plain text: one line per fixture, then the totals and the verdict."""
    totals = report.totals()
    lines = [
        (
            f"triage saving {report.token}: {report.triage}, "
            f"{_gate_text(report.question, report.threshold)}; judge gate off"
        ),
        (
            f"reviewers {', '.join(report.reviewers)}, judge {report.judge}; "
            f"${report.budget_usd:.2f} budget per run"
        ),
    ]
    if report.fixtures:
        width = max(len("fixture"), *(len(saving.fixture) for saving in report.fixtures))
        lines += [
            "",
            (
                f"  {'fixture':<{width}}  {'triage':>9}  {'no triage':>9}  {'jev':>9}  "
                f"{'decision':<9}  {'route':<22}  severity"
            ),
        ]
        for saving in report.fixtures:
            route = ",".join(saving.decision["route"]) if saving.decision else ""
            failed = "  (failed)" if saving.failed else ""
            lines.append(
                f"  {saving.fixture:<{width}}  {_usd(saving.with_triage.cost_usd):>9}  "
                f"{_usd(saving.no_triage.cost_usd):>9}  {_usd(saving.jev_cost_usd):>9}  "
                f"{_decision_text(saving.decision):<9}  {route or '-':<22}  "
                f"{saving.severity or 'none'}{failed}"
            )
    lines += [
        "",
        (
            f"{totals['fixtures']} fixtures: ${totals['with_triage_usd']:.4f} with triage, "
            f"${totals['no_triage_usd']:.4f} without (difference ${totals['difference_usd']:.4f}); "
            f"Jev overhead ${totals['jev_overhead_usd']:.4f}"
        ),
        (
            f"{totals['would_skip']} would skip; projected saving ${totals['projected_saving_usd']:.4f} "
            f"(skipped panel cost less Jev overhead); measurement spent "
            f"${totals['measurement_usd']:.4f}"
        ),
    ]
    if report.stopped:
        lines.append(f"\nstopped early: {report.stopped}")
    if report.failing:
        names = ", ".join(saving.fixture for saving in report.failing)
        lines.append(f"\nfailed runs: {names}")
    if report.would_skip_major:
        names = ", ".join(saving.fixture for saving in report.would_skip_major)
        lines.append(f"\nFAIL: would-skip fixtures with a {FAIL_ON} finding or worse: {names}")
    else:
        lines.append(f"\nOK: no would-skip fixture has a {FAIL_ON} finding or worse")
    return "\n".join(lines)


def _usd(cost: float) -> str:
    return f"${cost:.4f}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/triage_saving.py",
        description="Run the review panel over every fixture with and without diff-triage and "
        "report what skipping at the manifest's skip gate would save.",
        epilog="example: uv run python scripts/triage_saving.py --fixtures mixed,docs-only --json",
    )
    parser.add_argument(
        "--fixtures",
        type=lambda value: [name for name in value.split(",") if name],
        help="comma-separated fixtures to measure (default: all)",
    )
    parser.add_argument("--trace-db", type=Path, help="trace database (default: FACTORY_TRACE_DB)")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=DEFAULT_BUDGET_USD,
        help="budget of each panel run (default: %(default)s)",
    )
    parser.add_argument(
        "--max-usd",
        type=float,
        default=MAX_USD,
        help="stop starting fixtures once the spend, or its projection, passes this "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list the fixtures and agents; call nothing"
    )
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Any] = run,
    jev_runner: Callable[..., Any] = run_jev,
) -> int:
    """Measure the saving, print the report and return the exit code; the runners are for tests."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args, runner, jev_runner)
    except FactoryError as exc:
        if args.json:
            print(json.dumps(error_envelope(exc)), file=sys.stderr)
        else:
            hint = f" ({exc.hint})" if exc.hint else ""
            print(f"triage_saving: {exc.message}{hint}", file=sys.stderr)
        return exit_code(exc)


def _main(
    args: argparse.Namespace, runner: Callable[..., Any], jev_runner: Callable[..., Any]
) -> int:
    if args.budget_usd <= 0 or args.max_usd <= 0:
        raise UsageError("--budget-usd and --max-usd must be above 0")
    available = discover()
    names = args.fixtures or list(available)
    missing = [name for name in names if name not in available]
    if missing:
        raise FixtureNotFound(
            f"no fixture named {', '.join(repr(name) for name in missing)}",
            hint=f"known fixtures: {', '.join(available)}",
        )
    path = resolve_path(args.trace_db)
    panel = resolve_panel(
        allow_unpublished=args.allow_unpublished, trace_path=path, judge_gate=False
    )
    assert panel.triage is not None  # resolved with triage on
    gate = skip_gate(panel.triage)
    question = None if gate is None else gate.question
    if gate is not None and gate.option is not None:
        question = f"{gate.question}={gate.option}"
    threshold = None if gate is None else gate.value
    agents = {
        "reviewers": [spec.address for spec in panel.reviewers],
        "judge": panel.judge.address,
        "triage": panel.triage.address,
    }
    if args.dry_run:
        return _dry_run(args, names, agents, question, threshold)

    for env in (CLAUDE_KEY_ENV, JEV_KEY_ENV):
        if not os.environ.get(env):
            raise AuthOrConfigError(
                f"{env} is not set", hint=f"store it with `agb sys secrets set {env}` and export it"
            )
    built = build_fixtures(names, dest=REPOS)
    with Trace(path) as trace:
        measurement = measure(
            panel,
            built,
            trace,
            token=uuid.uuid4().hex[:8],
            budget_usd=args.budget_usd,
            max_usd=args.max_usd,
            runner=runner,
            jev_runner=jev_runner,
        )
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        raise FactoryError(
            f"could not open {path}: {str(exc).splitlines()[0]}",
            hint="a process writing to the store holds it; the runs are in it, read them later",
        ) from exc
    try:
        savings = read_back(con, measurement)
    except duckdb.CatalogException as exc:
        raise NotFoundError(f"{path} has no traces table", hint="pass --trace-db") from exc
    finally:
        con.close()

    report = Report(
        token=measurement.token,
        reviewers=tuple(agents["reviewers"]),
        judge=agents["judge"],
        triage=agents["triage"],
        question=question,
        threshold=threshold,
        budget_usd=args.budget_usd,
        max_usd=args.max_usd,
        fixtures=tuple(savings),
        stopped=measurement.stopped,
    )
    print(json.dumps(report.to_json()) if args.json else render(report))
    if report.would_skip_major:
        raise SavingFailed(
            f"{len(report.would_skip_major)} would-skip fixtures have a {FAIL_ON} finding or worse",
            hint="the skip threshold is too high (docs/jev-calibration.md)",
        )
    if report.failing:
        raise SavingFailed(
            f"{len(report.failing)} fixtures have a failed run",
            hint="their numbers are incomplete; see the report",
        )
    if report.stopped:
        raise SavingFailed(f"stopped early: {report.stopped}", hint="raise --max-usd")
    return 0


def _dry_run(
    args: argparse.Namespace,
    names: Sequence[str],
    agents: dict[str, Any],
    question: str | None,
    threshold: float | None,
) -> int:
    """Print what a measurement would run, built or not, and return 0. Nothing is called."""
    fixtures = [{"fixture": name, "built": (REPOS / name / ".git").exists()} for name in names]
    if args.json:
        document = {
            "dry_run": True,
            "agents": agents,
            "skip_gate": _gate_json(question, threshold),
            "budget_usd": args.budget_usd,
            "max_usd": args.max_usd,
            "fixtures": fixtures,
        }
        print(json.dumps(document))
        return 0
    lines = [
        (
            f"triage saving dry run: {len(fixtures)} fixtures, {len(MODES)} panel runs each, "
            "judge gate off"
        ),
        f"triage {agents['triage']}, {_gate_text(question, threshold)}",
        f"reviewers {', '.join(agents['reviewers'])}, judge {agents['judge']}",
        f"${args.budget_usd:.2f} budget per run, stop past ${args.max_usd:.2f} in all",
        "",
    ]
    lines += [
        f"  {item['fixture']}{'' if item['built'] else '  (not built yet)'}" for item in fixtures
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
