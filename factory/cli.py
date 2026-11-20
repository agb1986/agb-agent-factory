"""The `factory` console command."""

import argparse
import json
import sys
from collections.abc import Sequence
from importlib.metadata import version
from typing import get_args

from factory.exit import ExitCode, error_envelope, exit_code
from registry import list_agents
from registry.common_schemas import TicketKind
from runtime.work_items import SIZES, STATUSES


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
        epilog="example: factory review ./repo --pr 42 --comment",
    )
    review_parser.add_argument("repo_path", help="path of the git repository to review")
    review_parser.add_argument("--base", help="git ref the change starts from (with --head)")
    review_parser.add_argument("--head", help="git ref under review (with --base)")
    review_parser.add_argument(
        "--pr",
        type=int,
        help="review this pull request of the repository's origin instead of --base and --head",
    )
    review_parser.add_argument(
        "--comment",
        action="store_true",
        help="with --pr: post the findings on the pull request as a review, or edit the last one",
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --comment: read everything and send nothing, and say what would be posted",
    )
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
    plan_parser = sub.add_parser(
        "plan",
        help="write a PRD from an idea and, once approved, file its tickets in the work-item store",
        epilog="example: factory plan ./repo --idea @idea.md --json",
    )
    plan_parser.add_argument(
        "repo_path", help="path of the git repository to plan for; never changed"
    )
    plan_parser.add_argument(
        "--idea", help="the idea as text, or @FILE; starts a plan and stores its PRD"
    )
    plan_parser.add_argument(
        "--prd",
        help="@FILE, the PRD a first run stored (data/plans/<run-id>.md), to answer and approve",
    )
    plan_parser.add_argument(
        "--answer",
        dest="answers",
        action="append",
        default=[],
        metavar="QUESTION=ANSWER",
        help="answer an open question, named by its text or its number; repeat for more",
    )
    plan_parser.add_argument(
        "--approve",
        action="store_true",
        help="approve the PRD: break it into tickets and file them in the store, in todo",
    )
    plan_parser.add_argument(
        "--ref", default="HEAD", help="with --idea, git ref to plan against (default: %(default)s)"
    )
    plan_parser.add_argument(
        "--repository",
        help="with --idea, name the tickets give the repository (default: its absolute path)",
    )
    plan_parser.add_argument("--plans-dir", help="where the PRD files go (default: data/plans)")
    plan_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of text"
    )
    plan_parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )
    plan_parser.add_argument(
        "--budget-usd",
        type=float,
        help="stop the run once it has spent this many dollars (default: the pipeline default)",
    )
    _add_incident_parser(sub)
    run_parser = sub.add_parser(
        "run",
        help="run one LLM agent once on a JSON payload and print its output",
        epilog="example: factory run product-architect@^0.1 --input payload.json --repo . --json",
    )
    run_parser.add_argument("agent", help="`name@^MAJOR.MINOR`; the highest version if no range")
    run_parser.add_argument(
        "--input", required=True, help="file holding the JSON payload, or - to read it from stdin"
    )
    run_parser.add_argument(
        "--repo", default=".", help="repository the agent's tools read (default: the current one)"
    )
    run_parser.add_argument(
        "--allow-domain",
        dest="allow_domains",
        action="append",
        metavar="HOST",
        help="web host the agent may fetch, in place of the manifest's list; repeat for more",
    )
    run_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of text"
    )
    run_parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )
    run_parser.add_argument(
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
    deps_parser = sub.add_parser(
        "deps",
        help="print the import graph of a repository, or the modules that import some files",
        epilog="example: factory deps . --impact tools/_common.py --json",
    )
    deps_parser.add_argument("repo_path", help="the top of the work tree to read")
    deps_parser.add_argument(
        "--impact",
        nargs="+",
        action="extend",
        metavar="PATH",
        help="print the modules that import these files, transitively, with their depth",
    )
    deps_parser.add_argument(
        "--json", action="store_true", help="print one JSON document instead of text"
    )
    _add_ticket_parser(sub)
    _add_build_parser(sub)
    return parser


def _add_incident_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add `factory incident` to `sub`."""
    incident_parser = sub.add_parser(
        "incident",
        help="diagnose a Sentry event at its release, reproduce it and file an incident ticket",
        epilog="example: factory incident ./repo --event @issue.json --json",
    )
    incident_parser.add_argument(
        "repo_path", help="path of the git repository the event came from; never changed"
    )
    incident_parser.add_argument(
        "--event",
        required=True,
        help="the Sentry issue as `sentry.read_issue` returns it: JSON, or @FILE holding it",
    )
    incident_parser.add_argument(
        "--ref",
        help="git ref of the release (default: the event's release, as a tag or as v<release>)",
    )
    incident_parser.add_argument(
        "--repository",
        help="name the ticket gives the repository (default: its absolute path)",
    )
    incident_parser.add_argument(
        "--out-dir", help="where the report goes (default: data/incidents)"
    )
    incident_parser.add_argument(
        "--work-dir",
        help="directory for the throwaway clone (default: the system temporary directory)",
    )
    incident_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of text"
    )
    incident_parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )
    incident_parser.add_argument(
        "--budget-usd",
        type=float,
        help="stop the run once it has spent this many dollars (default: the pipeline default)",
    )


def _add_ticket_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add `factory ticket` and its six subcommands to `sub`."""
    ticket_parser = sub.add_parser(
        "ticket",
        help="add, list, show, move and link work items, and promote the ready ones",
        epilog="example: factory ticket list --status ready --json",
    )
    tsub = ticket_parser.add_subparsers(dest="ticket_command")

    def child(name: str, help: str, example: str) -> argparse.ArgumentParser:
        child_parser = tsub.add_parser(name, help=help, epilog=f"example: {example}")
        child_parser.add_argument(
            "--json", action="store_true", help="print one JSON document instead of text"
        )
        return child_parser

    add = child(
        "add",
        "write a new work item in todo",
        'factory ticket add --kind bug --title "Fix the parser" --acceptance "parses x" '
        "--repo agb1986/agb-agent-factory --ref main --json",
    )
    add.add_argument("--kind", required=True, choices=get_args(TicketKind))
    add.add_argument("--title", required=True, help="one line, at most 120 characters")
    add.add_argument("--body", default="", help="the problem and the context (default: empty)")
    add.add_argument(
        "--acceptance",
        required=True,
        action="append",
        metavar="CRITERION",
        help="one acceptance criterion; repeat for each",
    )
    add.add_argument("--repo", required=True, help="`owner/name` or an absolute path")
    add.add_argument("--ref", required=True, help="the branch or commit the work starts from")
    add.add_argument("--parent", help="`wi-<n>` or `prd:<pipeline_run_id>`")
    add.add_argument(
        "--depends-on",
        action="append",
        default=[],
        metavar="ID",
        help="a work item that must be done first; repeat for each",
    )
    add.add_argument(
        "--impacted-path",
        action="append",
        default=[],
        metavar="PATH",
        help="a repository-relative path the work touches; repeat for each",
    )
    add.add_argument("--size", choices=SIZES)
    add.add_argument("--reason", default="created", help="the creation event's reason")
    list_parser = child(
        "list", "list work items in id order", "factory ticket list --status ready --json"
    )
    list_parser.add_argument("--status", choices=STATUSES, help="only items in this state")
    show = child(
        "show", "print one work item with its cost and events", "factory ticket show wi-3 --json"
    )
    show.add_argument("id", help="the work item id, `wi-<n>`")
    move = child(
        "move",
        "move a work item to another state, as a human",
        'factory ticket move wi-3 dropped --reason "duplicate of wi-2"',
    )
    move.add_argument("id", help="the work item id, `wi-<n>`")
    move.add_argument("to", choices=STATUSES, help="the state to move to")
    move.add_argument("--reason", required=True, help="why; written to the event log")
    link = child(
        "link",
        "link a pipeline run to a work item, so its cost counts",
        "factory ticket link wi-3 3f2a9c --json",
    )
    link.add_argument("id", help="the work item id, `wi-<n>`")
    link.add_argument("pipeline_run_id", help="the pipeline run to link")
    child(
        "ready",
        "promote todo items whose dependencies are done, then list the ready ones in order",
        "factory ticket ready --json",
    )


def _add_build_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add `factory build` to `sub`."""
    build_parser = sub.add_parser(
        "build",
        help="build one ready work item in the sandbox, review it and open its pull request",
        epilog="example: factory build wi-3 --repo . --dry-run --json",
    )
    build_parser.add_argument("id", help="the work item id, `wi-<n>`; it must be ready")
    build_parser.add_argument(
        "--repo",
        help="the git repository to build in; it gains objects but no branch or tag, and with "
        "some git versions a remote-tracking ref for the pushed branch "
        "(default: the ticket's repo when it is a path, else the working directory)",
    )
    build_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every agent but open no pull request and move no work item",
    )
    build_parser.add_argument("--out-dir", help="where the report goes (default: data/builds)")
    build_parser.add_argument(
        "--work-dir",
        help="directory for the throwaway clone (default: the system temporary directory)",
    )
    build_parser.add_argument(
        "--budget-usd",
        type=float,
        help="stop the run once it has spent this many dollars (default: the pipeline default)",
    )
    build_parser.add_argument(
        "--json", action="store_true", help="print one JSON document on stdout instead of text"
    )
    build_parser.add_argument(
        "--allow-unpublished",
        action="store_true",
        help="also use agent versions that have not passed `factory publish-check`",
    )


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
        if args.command == "plan":
            return _plan(args)
        if args.command == "build":
            return _build(args)
        if args.command == "incident":
            return _incident(args)
        if args.command == "run":
            return _run(args)
        if args.command == "cost":
            return _cost(args)
        if args.command == "trace":
            return _trace(args)
        if args.command == "deps":
            return _deps(args)
        if args.command == "ticket" and args.ticket_command:
            return _ticket(args)
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


def _run(args: argparse.Namespace) -> int:
    """Run one LLM agent once; its output goes to stdout, the cost line to stderr without --json."""
    from runtime.run_once import command  # imported here: it pulls in the Anthropic SDK

    return command(
        args.agent,
        input=args.input,
        repo=args.repo,
        allow_domains=args.allow_domains,
        as_json=args.json,
        allow_unpublished=args.allow_unpublished,
        budget_usd=args.budget_usd,
    )


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
        pr=args.pr,
        comment=args.comment,
        dry_run=args.dry_run,
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


def _plan(args: argparse.Namespace) -> int:
    """Write a PRD from an idea, or approve a stored one and file its tickets."""
    from pipelines.plan_loop import command  # imported here: it pulls in the Anthropic SDK

    return command(
        args.repo_path,
        idea=args.idea,
        prd=args.prd,
        answers=args.answers,
        approve=args.approve,
        ref=args.ref,
        repository=args.repository,
        plans_dir=args.plans_dir,
        budget_usd=args.budget_usd,
        as_json=args.json,
        allow_unpublished=args.allow_unpublished,
    )


def _build(args: argparse.Namespace) -> int:
    """Build one ready work item and open its pull request; the report goes to stdout."""
    from pipelines.build_loop import command  # imported here: it pulls in the Anthropic SDK

    return command(
        args.id,
        repo=args.repo,
        dry_run=args.dry_run,
        out_dir=args.out_dir,
        work_dir=args.work_dir,
        budget_usd=args.budget_usd,
        as_json=args.json,
        allow_unpublished=args.allow_unpublished,
    )


def _incident(args: argparse.Namespace) -> int:
    """Diagnose one Sentry event at its release and file an incident ticket."""
    from pipelines.incident import command  # imported here: it pulls in the Anthropic SDK

    return command(
        args.repo_path,
        event=args.event,
        ref=args.ref,
        repository=args.repository,
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


def _deps(args: argparse.Namespace) -> int:
    """Print the import graph of a repository, or the impact set of the `--impact` files."""
    from tools.deps import command

    return command(args.repo_path, impact_paths=args.impact, as_json=args.json)


def _ticket(args: argparse.Namespace) -> int:
    """Run one `factory ticket` subcommand against the trace store's work items."""
    from runtime import ticket

    if args.ticket_command == "add":
        return ticket.add(
            kind=args.kind,
            title=args.title,
            body=args.body,
            acceptance=args.acceptance,
            repo=args.repo,
            ref=args.ref,
            parent=args.parent,
            depends_on=args.depends_on,
            impacted_paths=args.impacted_path,
            size=args.size,
            reason=args.reason,
            as_json=args.json,
        )
    if args.ticket_command == "list":
        return ticket.list_items(args.status, as_json=args.json)
    if args.ticket_command == "show":
        return ticket.show(args.id, as_json=args.json)
    if args.ticket_command == "move":
        return ticket.move(args.id, args.to, args.reason, as_json=args.json)
    if args.ticket_command == "link":
        return ticket.link(args.id, args.pipeline_run_id, as_json=args.json)
    return ticket.ready(as_json=args.json)


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
