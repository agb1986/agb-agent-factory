"""Identifiers a pipeline stamps on the trace rows of one invocation."""

import uuid


def new_pipeline_run_id() -> str:
    """Return a fresh pipeline run id: a random UUID in its canonical string form.

    A pipeline passes it to every `runtime.runner.run` call it makes, so the `traces` rows of one
    invocation share it and `Trace.cost_by_pipeline_run` can add them up.
    """
    return str(uuid.uuid4())
