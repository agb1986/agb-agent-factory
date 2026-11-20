"""Pick the next job to run from the pending ones."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    name: str
    priority: int


def next_job(pending: list[Job]) -> Job | None:
    """Return the pending job with the highest priority, or None when nothing is pending."""
    if not pending:
        return None
    return max(pending, key=lambda job: job.priority)
