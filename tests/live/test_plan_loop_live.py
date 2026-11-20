"""A live end-to-end run of the plan loop against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It plans a small feature on the built `storefront`
fixture in two steps: the idea makes product-architect write a PRD and stop, and the approval
makes story-breakdown cut it into tickets. It makes at least two requests. It checks that both
agents and the store events carry one pipeline run id, that every ticket is filed in `todo`, and
that the plan's cost is the sum of the two runs. The agents are resolved with `allow_unpublished`,
because the plan loop is run before their versions are published.
"""

from pathlib import Path

import pytest

from fixtures.build import build
from pipelines.plan_loop import AWAITING_ANSWERS, FILED, resolve_plan_agents, run_plan
from runtime.trace import Trace
from runtime.work_items import WorkItems

IDEA = "Let the storefront round prices to whole cents with decimal arithmetic instead of floats."


@pytest.mark.live
def test_an_idea_becomes_a_stored_prd_and_then_tickets_under_one_run_id(tmp_path: Path) -> None:
    agents = resolve_plan_agents(allow_unpublished=True)
    (fixture,) = build(["storefront"], dest=tmp_path / "repos")
    plans = tmp_path / "plans"
    with Trace(tmp_path / "trace.duckdb") as trace:
        first = run_plan(agents, fixture.path, trace, idea=IDEA, plans_dir=plans)
        assert first.prd_path.is_file()
        assert WorkItems(trace).list() == []
        # A PRD with open questions needs answers before it can be approved; the test answers
        # each with a stock reply so that the second step runs whatever the model asked.
        answers = [f"{n}=Use the simplest option." for n, _ in enumerate(first.prd.questions, 1)]
        second = run_plan(
            agents,
            fixture.path,
            trace,
            prd_source=first.prd_path.read_text(encoding="utf-8"),
            answers=answers,
            approve=True,
            plans_dir=plans,
        )
        assert second.status == FILED, second.status
        assert second.status != AWAITING_ANSWERS
        items = WorkItems(trace).list()
        rows = (
            trace._connect()
            .execute("SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces")
            .fetchall()
        )
        total = trace.cost_by_pipeline_run(first.pipeline_run_id)

    assert second.pipeline_run_id == first.pipeline_run_id
    assert items
    assert {item.status for item in items} == {"todo"}
    assert {item.parent for item in items} == {f"prd:{first.pipeline_run_id}"}
    assert len(rows) >= 2
    assert {row[1] for row in rows} == {first.pipeline_run_id}
    assert all(row[3] is None for row in rows)
    assert total == pytest.approx(sum(row[2] or 0 for row in rows))
