"""The `factory` console command."""

import argparse
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from factory.exit import ExitCode

AGENTS_DIR = Path("agents")


def list_agents(agents_dir: Path = AGENTS_DIR) -> list[str]:
    """Return the names of the agents found under `agents_dir`, sorted.

    A directory counts as an agent when it holds an `agent.yaml`. A missing `agents_dir` yields an
    empty list. This is a stub: #4 replaces it with the registry's resolver.
    """
    if not agents_dir.is_dir():
        return []
    return sorted(p.name for p in agents_dir.iterdir() if (p / "agent.yaml").is_file())


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the `factory` command."""
    parser = argparse.ArgumentParser(prog="factory", description="Run and inspect factory agents.")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('agb-agent-factory')}"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="list the agents found under agents/")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `factory` command and return its exit code.

    Returns 0 on success and 2 on a usage error, including a missing subcommand.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else ExitCode.USAGE
    if args.command == "list":
        for name in list_agents():
            print(name)
        return ExitCode.OK
    parser.print_usage(sys.stderr)
    return ExitCode.USAGE


if __name__ == "__main__":
    sys.exit(main())
