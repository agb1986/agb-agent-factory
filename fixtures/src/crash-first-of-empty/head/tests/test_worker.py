from jobs.queue import Job
from jobs.worker import run_once


def test_takes_the_highest_priority_job() -> None:
    pending = [Job("low", 1), Job("high", 5)]
    assert run_once(pending) == "high"
    assert pending == [Job("low", 1)]
