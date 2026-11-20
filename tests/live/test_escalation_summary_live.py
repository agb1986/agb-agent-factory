"""A live run of the escalation-summary agent against the real Messages API.

Needs `FACTORY_LIVE=1` and `ANTHROPIC_API_KEY`. It checks that the manifest's model exists, puts
every eval case to the agent, which makes one request per case, and checks that each answer passes
`runtime.escalation_summary.ungrounded`, the check that runs before a summary is stored: every
option's evidence is a span of the record and no dollar figure is invented. Each run must be
traced under its own run id with a cost. It then raises a record in a throwaway trace and checks
that `runtime.after_escalation.after_escalation` stores a summary for it under the run's budget.
It was not run as part of #94.
"""

from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.after_escalation import SUMMARY_AGENT, after_escalation
from runtime.budget import Budget
from runtime.escalation_summary import ungrounded
from runtime.escalations import get_summary
from runtime.runner import run
from runtime.trace import Trace
from tests.runtime.test_escalation_summary import raise_one

CASES = AGENTS_DIR / "escalation-summary" / "evals" / "cases.yaml"


def _agent() -> LLMSpec:
    name, version_range = SUMMARY_AGENT
    spec = resolve(name, version_range, published_only=False)
    assert isinstance(spec, LLMSpec)
    assert (spec.model, spec.effort) == ("claude-haiku-5-5", "low")
    return spec


@pytest.mark.live
def test_every_case_gets_a_grounded_summary_and_is_traced(tmp_path: Path) -> None:
    spec = _agent()
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    cases = load_cases(CASES)
    with Trace(tmp_path / "trace.duckdb") as trace:
        answers = {
            case.id: run(spec, case.payload, trace, pipeline_run_id=f"live-{case.id}").model_dump()
            for case in cases
        }
        rows = (
            trace._connect()
            .execute("SELECT run_id, cost_usd, error FROM traces ORDER BY started_at")
            .fetchall()
        )

    for case in cases:
        answer = answers[case.id]
        assert answer["paragraph"].strip(), case.id
        assert ungrounded(answer, case.payload) == [], (case.id, answer)
    assert len(rows) == len(cases)
    assert len({row[0] for row in rows}) == len(cases)
    for row in rows:
        assert row[2] is None
        assert row[1] is not None and row[1] > 0


@pytest.mark.live
def test_a_raised_record_gets_a_stored_summary_under_the_budget(tmp_path: Path) -> None:
    spec = _agent()
    budget = Budget(1.0)
    with Trace(tmp_path / "trace.duckdb") as trace:
        record = raise_one(trace)
        after = after_escalation(
            trace,
            record,
            budget=budget,
            pipeline_run_id="p1",
            dry_run=False,
            summary_agent=spec,
        )
        stored = get_summary(trace, record.id)
        [row] = (
            trace._connect()
            .execute("SELECT run_id, pipeline_run_id, cost_usd, error FROM traces")
            .fetchall()
        )

    assert after.summary.done, after.summary.reason
    assert stored is not None
    assert (stored.run_id, stored.agent_ref) == (after.summary.run_id, spec.address)
    assert row[0] == after.summary.run_id
    assert row[1] == "p1"
    assert row[3] is None
    assert row[2] is not None and row[2] > 0
    assert budget.spent == pytest.approx(row[2])
