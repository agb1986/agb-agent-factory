"""Pick the next job to run from the pending ones."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    name: str
    priority: int


def next_job(pending: list[Job]) -> Job | None:
    """Return the pending job with the highest priority, the first one listed on a tie."""
    ranked = sorted(pending, key=lambda job: -job.priority)
    return ranked[0]
