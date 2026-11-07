"""Live check of the `rubric` grader against the real Messages API.

Skipped unless `FACTORY_LIVE=1` and `ANTHROPIC_API_KEY` are set. It runs one `evaluate` with two
rubric cases on the code-reviewer spec: the agent is a fake that answers instantly, so the only
model calls are the rubric grader's, two Haiku requests (four if one is retried). A summary that
meets every criterion must pass, a summary that meets none must fail, and both calls must be
traced with their cost under the eval's pipeline run id.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from evals.cases import Case, Criterion, Expected, Rubric
from evals.graders.rubric import RUBRIC_REF
from evals.run import evaluate
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.trace import Trace

RUBRIC = Rubric(
    criteria=(
        Criterion(
            id="names-the-bug",
            text="The summary says the rolling window is off by one, covering one extra element.",
            required=True,
            floor=0.5,
        ),
        Criterion(id="says-where", text="The summary names the file series/rolling.py."),
        Criterion(id="gives-the-fix", text="The summary says how to fix the slice bound."),
    ),
    threshold=0.7,
    required_fields=("summary",),
    forbidden=("TODO",),
)
SUMMARIES = {
    "good": (
        "The rolling window in series/rolling.py is off by one: the slice ends at len(window) + 1, "
        "so each mean covers one extra element. End the slice at len(window) instead."
    ),
    "bad": "The change looks fine to me and I have nothing to add.",
}


class SummaryRunner:
    """Stands in for the agent: answers each case with its summary from `SUMMARIES`."""

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        summary = SUMMARIES[payload["base"]]
        return spec.output_class.model_validate(
            {"verdict": "request_changes", "findings": [], "summary": summary}
        )


@pytest.mark.live
def test_the_rubric_grader_passes_a_good_summary_and_fails_a_bad_one(tmp_path: Path) -> None:
    spec = resolve("code-reviewer", "^1.2")
    assert isinstance(spec, LLMSpec)
    repo = tmp_path / "off-by-one"
    repo.mkdir()
    cases = [
        Case(
            id=name,
            fixture="off-by-one",
            payload={"base": name, "head": "head"},
            expected=Expected(verdict=None, rubric=RUBRIC),
            grader="rubric",
        )
        for name in SUMMARIES
    ]
    with Trace(tmp_path / "trace.duckdb") as trace:
        report = evaluate(
            spec,
            cases,
            trace,
            runner=SummaryRunner(),
            fixture_paths={"off-by-one": repo},
            eval_run_id="live-rubric",
        )
        rows = (
            trace._connect()
            .execute("SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces")
            .fetchall()
        )
    good, bad = report.results
    for result in report.results:
        print(result.case.id, result.passed, result.grade and result.grade.reason, result.error)
    assert good.error is None and bad.error is None
    assert good.passed
    assert not bad.passed
    assert rows
    assert all(row[0] == RUBRIC_REF and row[1] == "live-rubric" for row in rows)
    assert all(row[2] is not None and row[2] > 0 and row[3] is None for row in rows)
    assert report.cost_usd > 0
