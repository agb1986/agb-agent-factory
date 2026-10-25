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
