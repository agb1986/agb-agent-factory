"""A live end-to-end run of the product-architect agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It writes one PRD for the `contradictory-free-shipping`
eval case against the built `mixed` fixture, which makes at least one request to the manifest's
model at the manifest's effort, and checks that the answer has no goals and asks which rule wins,
that its related files exist, and that the run is traced with a cost.
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
def test_a_contradictory_request_gets_open_questions_and_no_goals(tmp_path: Path) -> None:
    spec = resolve("product-architect", "^0.1")
    assert isinstance(spec, LLMSpec)
    # A missing binary ends a run as soon as the model calls its tool, so a tool whose binary is
    # not on PATH is left out; the agent works with the git and deps tools alone.
    missing = {
        tool
        for tool, binary in (("rg.search", "rg"), ("memory.search", "agb"))
        if shutil.which(binary) is None
    }
    if missing:
        allowed = tuple(name for name in spec.tools.allow if name not in missing)
        spec = spec.model_copy(update={"tools": ToolsSpec(allow=allowed, deny=tuple(missing))})
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "product-architect" / "evals" / "cases.yaml")
        if case.id == "contradictory-free-shipping"
    ]
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    payload = dict(case.payload) | {"repo_path": str(fixture.path)}
    with Trace(tmp_path / "trace.duckdb") as trace:
        prd = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-prd-1")
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(prd, spec.output_class)
    found = prd.model_dump()
    assert found["goals"] == [], found
    assert found["open_questions"], found
    for path in found["related_files"]:
        assert (fixture.path / path).exists(), path

    assert len(rows) == 1
    agent_ref, pipeline_run_id, cost_usd, error = rows[0]
    assert agent_ref == "product-architect@0.1.0"
    assert pipeline_run_id == "live-prd-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0
