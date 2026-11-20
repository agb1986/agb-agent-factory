"""Keeping issues in memory."""

from tracker.models import Issue

_ISSUES: dict[int, Issue] = {}


def add(title: str, labels: list[str] | None = None) -> Issue:
    """Create an issue with the next number and store it."""
    issue = Issue(len(_ISSUES) + 1, title, labels=list(labels or []))
    _ISSUES[issue.number] = issue
    return issue


def get(number: int) -> Issue:
    """Return an issue, or raise KeyError."""
    return _ISSUES[number]


def all_issues() -> list[Issue]:
    """Return every issue in number order."""
    return [_ISSUES[number] for number in sorted(_ISSUES)]


def clear() -> None:
    """Forget every issue; the tests use it."""
    _ISSUES.clear()
