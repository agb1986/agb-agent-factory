"""Summaries of the tracker."""

from tracker.storage import all_issues


def counts() -> dict[str, int]:
    """Return how many issues are in each state."""
    found: dict[str, int] = {}
    for issue in all_issues():
        found[issue.state] = found.get(issue.state, 0) + 1
    return found


def summary() -> str:
    """Return the counts as one line."""
    return ", ".join(f"{state}: {count}" for state, count in sorted(counts().items()))
