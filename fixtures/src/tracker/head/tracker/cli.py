"""Command line entry point."""

import sys

from tracker.report import summary
from tracker.search import by_label
from tracker.storage import add


def main(argv: list[str]) -> int:
    """Run `add TITLE`, `label NAME` or `summary`; return the exit code."""
    if argv[:1] == ["add"] and len(argv) > 1:
        print(add(" ".join(argv[1:])).number)
        return 0
    if argv[:1] == ["label"] and len(argv) == 2:
        print("\n".join(issue.title for issue in by_label(argv[1])))
        return 0
    if argv[:1] == ["summary"]:
        print(summary())
        return 0
    print("usage: tracker add TITLE | label NAME | summary", file=sys.stderr)
    return 2
