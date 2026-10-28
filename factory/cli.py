"""The `factory` console command."""

import argparse
import json
import sys
from collections.abc import Sequence
from importlib.metadata import version

from factory.exit import ExitCode, error_envelope, exit_code
from registry import list_agents


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the `factory` command."""
    parser = argparse.ArgumentParser(prog="factory", description="Run and inspect factory agents.")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('agb-agent-factory')}"
    )
    sub = parser.add_subparsers(dest="command")
    list_parser = sub.add_parser("list", help="list every agent version found under agents/")
    list_parser.add_argument(
        "--json", action="store_true", help="print one JSON document instead of text"
    )
    eval_parser = sub.add_parser(
        "eval",
        help="run an agent's eval cases and gate on its min_pass_rate",
        epilog="example: factory eval code-reviewer@^1.2 --json --workers 4",
    )
    eval_parser.add_argument("agent", help="`name@^MAJOR.MINOR`; the highest version if no range")
    eval_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of a table"
    )
    eval_parser.add_argument(
        "--workers", type=int, default=4, help="cases to run at once (default: %(default)s)"
    )
    publish_parser = sub.add_parser(
        "publish-check",
        help="run an agent's eval cases and record the version as published when they pass",
        epilog="example: factory publish-check code-reviewer@^1.2 --json",
    )
    publish_parser.add_argument(
        "agent", help="`name@^MAJOR.MINOR`; the highest version if no range"
    )
    publish_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of a table"
    )
    publish_parser.add_argument(
        "--workers", type=int, default=4, help="cases to run at once (default: %(default)s)"
    )
    review_parser = sub.add_parser(
        "review",
        help="review a change with the reviewer panel and the judge",
        epilog="example: factory review ./repo --base main --head feature --json",
    )
    review_parser.add_argument("repo_path", help="path of the git repository to review")
    review_parser.add_argument("--base", required=True, help="git ref the change starts from")
    review_parser.add_argument("--head", required=True, help="git ref under review")
    review_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of text"
    )
    review_parser.add_argument(
        "--reviewers",
        type=lambda value: value.split(","),
        help="comma-separated reviewers to run (default: every reviewer of the panel)",
    )
    review_parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )
    review_parser.add_argument(
        "--no-triage", action="store_true", help="do not call the Jev triage agent"
    )
    review_parser.add_argument(
        "--no-judge-gate",
        action="store_true",
        help="do not call the Jev judge-gate agent, so the judge always runs",
    )
    review_parser.add_argument(
        "--budget-usd",
        type=float,
        help="stop the review once it has spent this many dollars (default: the pipeline default)",
    )
    triage_parser = sub.add_parser(
        "triage",
        help="run only the Jev triage of a change and print its decision",
        epilog="example: factory triage ./repo --base main --head feature --json",
    )
    triage_parser.add_argument("repo_path", help="path of the git repository to triage")
    triage_parser.add_argument("--base", required=True, help="git ref the change starts from")
    triage_parser.add_argument("--head", required=True, help="git ref under review")
    triage_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of text"
    )
    triage_parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )
    fix_parser = sub.add_parser(
        "fix",
        help="write a test for one finding, fix the code in the sandbox and review the fix",
        epilog="example: factory fix ./repo --finding @review.json --finding-index 0 --json",
    )
    fix_parser.add_argument("repo_path", help="path of the git repository to fix; never changed")
    fix_parser.add_argument(
        "--finding",
        required=True,
        help="the finding as JSON, a `factory review --json` document, or @FILE holding either",
    )
    fix_parser.add_argument(
        "--finding-index",
        type=int,
        default=0,
        help="which of the judge's merged findings of a review document to fix "
        "(default: %(default)s)",
    )
    fix_parser.add_argument(
        "--head", default="HEAD", help="git ref to start from (default: %(default)s)"
    )
    fix_parser.add_argument(
        "--out-dir", help="where the patch and the report go (default: data/fixes)"
    )
    fix_parser.add_argument(
        "--work-dir",
        help="directory for the throwaway clone (default: the system temporary directory)",
    )
    fix_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of text"
    )
    fix_parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )
    fix_parser.add_argument(
        "--budget-usd",
        type=float,
        help="stop the run once it has spent this many dollars (default: the pipeline default)",
    )
    cost_parser = sub.add_parser(
        "cost",
        help="print what the agent runs in the trace cost since a date",
        epilog="example: factory cost --since 2026-10-01 --by pipeline --json",
    )
    cost_parser.add_argument(
        "--since", required=True, help="start date or datetime, in UTC if it has no offset"
    )
    cost_parser.add_argument(
        "--by",
        choices=("agent", "pipeline", "kind"),
        default="agent",
        help="group by agent version, pipeline run or runner kind (default: agent)",
    )
    cost_parser.add_argument(
        "--json", action="store_true", help="print one JSON document instead of text"
    )
    trace_parser = sub.add_parser(
        "trace",
        help="export one pipeline run as an Agent Flow JSONL event log",
        epilog="example: factory trace 3f2a9c --out /tmp/run.jsonl",
    )
    trace_parser.add_argument("pipeline_run_id", help="the pipeline run id to export")
    trace_parser.add_argument("--out", help="file to write (default: stdout)")
    trace_parser.add_argument(
        "--stretch",
        type=float,
        default=1.0,
        help="multiply every event time by this, to slow a short run down for viewing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `factory` command and return its exit code.

    Returns 0 on success, 1 for a negative result such as a pass rate below the minimum, and 2 on a
    usage error, including a missing subcommand. Any exception a command raises is mapped to its
    exit code; the message goes to stderr, as the one-line JSON envelope under `--json`.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else ExitCode.USAGE
    try:
        if args.command == "list":
            return _list(as_json=args.json)
        if args.command == "eval":
            return _eval(args.agent, as_json=args.json, workers=args.workers)
        if args.command == "publish-check":
            return _publish_check(args.agent, as_json=args.json, workers=args.workers)
        if args.command == "review":
            return _review(args)
        if args.command == "triage":
            return _triage(args)
        if args.command == "fix":
            return _fix(args)
        if args.command == "cost":
            return _cost(args)
        if args.command == "trace":
            return _trace(args)
    except Exception as exc:  # noqa: BLE001 - the root handler maps every failure to a code
        return _fail(exc, as_json=getattr(args, "json", False))
    parser.print_usage(sys.stderr)
    return ExitCode.USAGE


def _list(*, as_json: bool) -> int:
    """Print every agent version, as `name@version<TAB>kind` lines or as `{"agents": [...]}`."""
    agents = list_agents()
    if as_json:
        print(json.dumps({"agents": [agent._asdict() for agent in agents]}))
    else:
        for agent in agents:
            print(f"{agent.name}@{agent.version}\t{agent.kind}")
    return ExitCode.OK


def _eval(agent: str, *, as_json: bool, workers: int) -> int:
    """Run an agent's eval cases; the table goes to stderr, the JSON document to stdout."""
    from evals.run import command  # imported here: it pulls in the Anthropic SDK

    return command(agent, as_json=as_json, workers=workers)


def _publish_check(agent: str, *, as_json: bool, workers: int) -> int:
    """Evaluate an agent and write a `published` row if it passes; same output as `eval`."""
    from registry.published import command  # imported here: it pulls in the Anthropic SDK

    return command(agent, as_json=as_json, workers=workers)


def _review(args: argparse.Namespace) -> int:
    """Run the review panel on a change; the text or JSON report goes to stdout."""
    from pipelines.review_panel import command  # imported here: it pulls in the Anthropic SDK

    return command(
        args.repo_path,
        base=args.base,
        head=args.head,
        as_json=args.json,
        reviewers=args.reviewers,
        allow_unpublished=args.allow_unpublished,
        no_triage=args.no_triage,
        no_judge_gate=args.no_judge_gate,
        budget_usd=args.budget_usd,
    )


def _triage(args: argparse.Namespace) -> int:
    """Run only the triage step on a change; the decision goes to stdout."""
    from pipelines.triage import command  # imported here: it pulls in the Jev runner

    return command(
        args.repo_path,
        base=args.base,
        head=args.head,
        as_json=args.json,
        allow_unpublished=args.allow_unpublished,
    )


def _fix(args: argparse.Namespace) -> int:
    """Fix one finding in a throwaway clone and review the fix; the report goes to stdout."""
    from pipelines.fix_loop import command  # imported here: it pulls in the Anthropic SDK

    return command(
        args.repo_path,
        finding=args.finding,
        finding_index=args.finding_index,
        head=args.head,
        out_dir=args.out_dir,
        work_dir=args.work_dir,
        budget_usd=args.budget_usd,
        as_json=args.json,
        allow_unpublished=args.allow_unpublished,
    )


def _cost(args: argparse.Namespace) -> int:
    """Print the spend recorded in the trace since a date, by agent, pipeline run or kind."""
    from runtime.cost import command

    return command(args.since, by=args.by, as_json=args.json)


def _trace(args: argparse.Namespace) -> int:
    """Write the Agent Flow event log for one pipeline run."""
    from runtime.agent_flow import command

    return command(args.pipeline_run_id, out=args.out, stretch=args.stretch)


def _fail(exc: Exception, *, as_json: bool) -> int:
    """Report `exc` on stderr and return its exit code. Stdout stays untouched."""
    if as_json:
        print(json.dumps(error_envelope(exc)), file=sys.stderr)
    else:
        print(getattr(exc, "message", None) or str(exc) or type(exc).__name__, file=sys.stderr)
        hint = getattr(exc, "hint", None)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
    return exit_code(exc)


if __name__ == "__main__":
    sys.exit(main())
