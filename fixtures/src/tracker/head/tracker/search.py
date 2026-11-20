"""Finding issues."""

from tracker.models import Issue
from tracker.storage import all_issues


def by_label(label: str) -> list[Issue]:
    """Return the issues that carry `label`."""
    return [issue for issue in all_issues() if label in issue.labels]


def by_word(word: str) -> list[Issue]:
    """Return the issues whose title contains `word`, ignoring case."""
    return [issue for issue in all_issues() if word.lower() in issue.title.lower()]
