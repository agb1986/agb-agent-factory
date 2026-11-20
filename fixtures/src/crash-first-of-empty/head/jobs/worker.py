"""Run one step of the worker loop."""

from jobs.queue import Job, next_job


def run_once(pending: list[Job]) -> str:
    """Take the next job and return its name, or "idle" when nothing is pending."""
    job = next_job(pending)
    if job is None:
        return "idle"
    pending.remove(job)
    return job.name
