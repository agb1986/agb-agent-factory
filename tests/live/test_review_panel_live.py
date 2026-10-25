"""A live end-to-end run of the review panel against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It reviews the `mixed` fixture with the three reviewers
and the judge, which makes at least four requests, and checks that the four runs are traced under
one pipeline run id and that the pipeline's cost is the sum of their costs. The agents are
resolved with `allow_unpublished`, because the panel is run before the versions are published.
"""

import shutil
from pathlib import Path

import anthropic
import pytest

from fixtures.build import build
from pipelines.review_panel import Panel, resolve_panel, run_panel
from registry.spec import LLMSpec, ToolsSpec
from runtime.trace import Trace


def _without_rg(spec: LLMSpec) -> LLMSpec:
    # A missing `rg` ends a run as soon as the model calls `rg.search`, so without it the agent
    # runs with its other tools, which is a supported configuration.
    if shutil.which("rg") is not None or "rg.search" not in spec.tools.allow:
        return spec
    allowed = tuple(name for name in spec.tools.allow if name != "rg.search")
    return spec.model_copy(update={"tools": ToolsSpec(allow=allowed, deny=("rg.search",))})


@pytest.mark.live
def test_the_panel_traces_four_runs_under_one_pipeline_run_id(tmp_path: Path) -> None:
    resolved = resolve_panel(allow_unpublished=True)
    panel = Panel(
        reviewers=tuple(_without_rg(spec) for spec in resolved.reviewers),
        judge=_without_rg(resolved.judge),
    )
    with anthropic.Anthropic() as client:
        available = {model.id for model in client.models.list()}
    for spec in (*panel.reviewers, panel.judge):
        assert spec.model in available, spec.address

    (fixture,) = build(["mixed"], dest=tmp_path / "repos")
    with Trace(tmp_path / "trace.duckdb") as trace:
        result = run_panel(
            panel, fixture.path, "base", "head", trace, pipeline_run_id="live-panel-1"
        )
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )
        total = trace.cost_by_pipeline_run("live-panel-1")

    assert result.pipeline_run_id == "live-panel-1"
    assert len(rows) == 4
    assert {row[1] for row in rows} == {"live-panel-1"}
    assert {row[0] for row in rows} == {spec.address for spec in (*panel.reviewers, panel.judge)}
    assert all(row[3] is None for row in rows)
    assert all(row[2] is not None and row[2] > 0 for row in rows)
    assert total == pytest.approx(sum(row[2] for row in rows))
    assert result.total_cost_usd == pytest.approx(total)
    assert all(reviewer.review is not None for reviewer in result.reviewers)
    assert isinstance(result.ship, bool)
