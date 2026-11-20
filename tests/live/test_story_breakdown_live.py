"""A live end-to-end run of the story-breakdown agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It breaks down the `storefront-money-decimal` eval
case's PRD against the built `storefront` fixture, which makes at least one request to the
manifest's model at the manifest's effort, and checks that the tickets form a valid plan (the
output model already rejects a cycle or a bad index), that every impacted path exists, that no two
unordered tickets share a path, and that the run is traced with a cost.
"""

import shutil
from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from fixtures.build import build
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec, ToolsSpec
from runtime.runner import build_request, run
from runtime.trace import Trace


@pytest.mark.live
def test_a_prd_becomes_ordered_tickets_with_disjoint_parallel_paths(tmp_path: Path) -> None:
    spec = resolve("story-breakdown", "^0.1")
    assert isinstance(spec, LLMSpec)
    # A missing binary ends a run as soon as the model calls its tool, so a tool whose binary is
    # not on PATH is left out; the agent works with the git and deps tools alone.
    if shutil.which("rg") is None:
        allowed = tuple(name for name in spec.tools.allow if name != "rg.search")
        spec = spec.model_copy(update={"tools": ToolsSpec(allow=allowed, deny=("rg.search",))})
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "story-breakdown" / "evals" / "cases.yaml")
        if case.id == "storefront-money-decimal"
    ]
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    payload = dict(case.payload) | {"repo_path": str(fixture.path)}
    with Trace(tmp_path / "trace.duckdb") as trace:
        plan = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-plan-1")
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(plan, spec.output_class)
    tickets = plan.model_dump()["tickets"]
    assert tickets, plan
    for ticket in tickets:
        assert ticket["repo"] == "acme/storefront" and ticket["ref"] == "head", ticket
        assert ticket["acceptance"], ticket
        assert ticket["impacted_paths"], ticket
        for path in ticket["impacted_paths"]:
            assert (fixture.path / path).exists(), path
    assert plan.parallel_conflicts() == [], plan.parallel_conflicts()  # ty: ignore[unresolved-attribute]

    assert len(rows) == 1
    agent_ref, pipeline_run_id, cost_usd, error = rows[0]
    assert agent_ref == "story-breakdown@0.1.0"
    assert pipeline_run_id == "live-plan-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0
