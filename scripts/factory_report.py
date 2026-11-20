"""Report factory metrics for a range of UTC days from the trace store and the work-item store.

Usage: `uv run python scripts/factory_report.py [options]`, run from the repository root, once, with
no scheduler. It reports tickets per state, the cost per ticket and per lifecycle phase, the panel's
ship rate, escalations per ticket, the time tickets spent in each state, the rubric grader's scores
per day and the cost per agent version, as Markdown or, with `--json`, as one JSON document. The
queries live in `runtime/reporting.py`, which also says what each measure reads; this script only
parses arguments and prints.

The window runs from `--since` to `--until`, both UTC days, both included. The ship decision of the
panel's runs uses the `scoring` block of the judge manifest, `agents/judge/agent.yaml` unless
`--judge-manifest` says otherwise. The store is opened read-only. Nothing prints a prompt, a diff or
a finding's text, only counts, scores, costs and ticket ids.

A window without a trace row or a work-item event says so and exits 1: a report of zeros is not a
result. Exit codes follow `factory`: 0 the report has data, 1 the window is empty or the store
cannot be opened, 2 bad arguments, 3 no trace store, 4 a bad manifest.
"""

import argparse
import datetime
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from factory.exit import FactoryError, UsageError, error_envelope, exit_code
from registry import LLMSpec, load_spec
from runtime.reporting import UNATTRIBUTED, FactoryReport, factory_report, open_store
from runtime.trace import resolve_path

JUDGE_MANIFEST = Path("agents/judge/agent.yaml")
REPORT_DAYS = 7
"""How many days the report covers when `--since` is not given, `--until` included."""


def render(report: FactoryReport) -> str:
    """Return the report as Markdown, or a line saying the window is empty.

    The window is printed as the UTC days it covers, the last day included.
    """
    last = report.end - datetime.timedelta(days=1)
    title = f"# Factory report, {report.start.date()} to {last.date()} (UTC)"
    if report.empty:
        return (
            f"{title}\n\nNo activity in this window: no trace row and no work-item event. "
            "Nothing to report."
        )
    lines = [
        title,
        "",
        f"{report.trace_rows} trace rows and {report.events} work-item events in the window.",
        "",
        "## Tickets per state, at the end of the window",
        "",
        *(f"- {state}: {count}" for state, count in report.tickets.items()),
        "",
        "## Time in each state",
        "",
    ]
    lines += [
        f"- {entry.state}: {_duration(entry.seconds)} over {entry.tickets} tickets"
        for entry in report.time_in_state
    ]
    esc = report.escalations
    per_ticket = "n/a" if esc.per_ticket is None else f"{esc.per_ticket:.2f}"
    lines += [
        "",
        "## Escalations",
        "",
        f"- moves into escalated: {esc.escalated}",
        f"- moves into blocked: {esc.blocked}",
        f"- tickets with an event in the window: {esc.tickets}",
        f"- escalations per ticket: {per_ticket}",
        "",
        "## Cost of the tickets' runs",
        "",
        f"- total: {_usd(report.costs.total_usd)}",
        f"- mean per ticket: {_usd(report.costs.mean_per_ticket_usd)}",
    ]
    if report.costs.per_phase:
        lines += ["", "Per lifecycle phase (the state the ticket was in when the run started):", ""]
        lines += [
            f"- {phase}{' (before the ticket existed)' if phase == UNATTRIBUTED else ''}: "
            f"{_usd(cost)}"
            for phase, cost in report.costs.per_phase.items()
        ]
        lines += ["", "Per ticket:", ""]
        lines += [f"- {item}: {_usd(cost)}" for item, cost in report.costs.per_ticket.items()]
    panel = report.panel
    lines += ["", "## Review panel", ""]
    if panel.ship_rate is None:
        lines.append("- no panel run with a verdict in the window")
    else:
        lines += [
            f"- ship rate: {panel.ship_rate:.0%} ({panel.shipped} of {panel.runs} runs)",
            f"- judged: {panel.judged}, judge skipped by the gate: {panel.skipped}",
            f"- mean weighted score of the judged runs: {_score(panel.mean_score)}",
        ]
    if panel.unreadable:
        lines.append(f"- judge outputs without the four scores, left out: {panel.unreadable}")
    lines += ["", "## Rubric scores per day", ""]
    if report.rubric:
        lines += [
            f"- {day.day}: mean {_score(day.mean_score)} over {day.scores} scores, "
            f"{day.passed} passed"
            for day in report.rubric
        ]
    else:
        lines.append("- no rubric score in the window")
    lines += ["", "## Cost per agent version", ""]
    lines += [
        f"- {agent.agent}@{agent.version}: {_usd(agent.cost_usd)}, {agent.runs} runs, "
        f"{agent.errors} errors"
        for agent in report.agents
    ] or ["- no trace row in the window"]
    lines += ["", "Agents are not grouped by team: the registry has no `team` field yet."]
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    hours, rest = divmod(round(seconds), 3600)
    return f"{hours}h {rest // 60}m"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _date(flag: str, text: str | None, default: datetime.date) -> datetime.date:
    if text is None:
        return default
    try:
        return datetime.date.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(f"{flag}: {exc}", hint="pass a date such as 2026-11-02") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/factory_report.py",
        description="Report factory metrics from the trace store and the work-item store: tickets "
        "per state, cost, panel ship rate, escalations, time in state, rubric scores.",
        epilog="example: uv run python scripts/factory_report.py --since 2026-11-02 --json",
    )
    parser.add_argument(
        "--since",
        help=f"first UTC day, YYYY-MM-DD (default: the {REPORT_DAYS} days ending at --until)",
    )
    parser.add_argument("--until", help="last UTC day, YYYY-MM-DD, included (default: today)")
    parser.add_argument(
        "--judge-manifest",
        type=Path,
        default=JUDGE_MANIFEST,
        help="the judge manifest whose scoring block decides ship",
    )
    parser.add_argument("--trace", type=Path, help="trace database (default: FACTORY_TRACE_DB)")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def main(argv: Sequence[str] | None = None, *, today: datetime.date | None = None) -> int:
    """Print the factory report and return the exit code; `today` is for tests."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _report(args, today or datetime.datetime.now(datetime.UTC).date())
    except FactoryError as exc:
        if args.json:
            print(json.dumps(error_envelope(exc)), file=sys.stderr)
        else:
            hint = f" ({exc.hint})" if exc.hint else ""
            print(f"factory_report: {exc.message}{hint}", file=sys.stderr)
        return exit_code(exc)


def _report(args: argparse.Namespace, today: datetime.date) -> int:
    until = _date("--until", args.until, today)
    since = _date("--since", args.since, until - datetime.timedelta(days=REPORT_DAYS - 1))
    if since > until:
        raise UsageError(f"--since {since} is after --until {until}")
    spec = load_spec(args.judge_manifest)
    if not isinstance(spec, LLMSpec) or spec.scoring is None:
        raise UsageError(
            f"{args.judge_manifest} is not an LLM manifest with a scoring block",
            hint="pass --judge-manifest agents/judge/agent.yaml",
        )
    start = datetime.datetime.combine(since, datetime.time(), datetime.UTC)
    end = datetime.datetime.combine(
        until + datetime.timedelta(days=1), datetime.time(), datetime.UTC
    )
    with open_store(resolve_path(args.trace)) as con:
        report = factory_report(con, start, end, spec.scoring)
    print(json.dumps(report.to_json()) if args.json else render(report))
    return 1 if report.empty else 0


if __name__ == "__main__":
    sys.exit(main())
