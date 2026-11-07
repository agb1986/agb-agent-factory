"""A live end-to-end run of the benchmark agent against the real Messages API and the real web.

Skipped unless `ANTHROPIC_API_KEY` is set. It compares the two PDF libraries of the
`pdf-agpl-ruled-out` eval case against the built `clean-refactor` fixture, which makes at least one
request to the manifest's model at the manifest's effort, and checks that the AGPL candidate is
ruled out, that every fact in the table carries a citation, and that the run is traced with a cost.
"""

import shutil
from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from fixtures.build import build
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request, run
from runtime.trace import Trace


@pytest.mark.live
def test_an_agpl_candidate_is_ruled_out_with_cited_facts(tmp_path: Path) -> None:
    spec = resolve("benchmark", "^0.1")
    assert isinstance(spec, LLMSpec)
    # The web tools and memory.search run through agb, so without it the run cannot work at all.
    if shutil.which("agb") is None:
        pytest.skip("agb is not on PATH")
    # A missing binary ends a run as soon as the model calls its tool, so rg.search is left out
    # when rg is not on PATH; the agent still has the web and git tools.
    if shutil.which("rg") is None:
        allowed = tuple(name for name in spec.tools.allow if name != "rg.search")
        tools = spec.tools.model_copy(update={"allow": allowed, "deny": ("rg.search",)})
        spec = spec.model_copy(update={"tools": tools})
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "benchmark" / "evals" / "cases.yaml")
        if case.id == "pdf-agpl-ruled-out"
    ]
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    payload = dict(case.payload) | {"repo_path": str(fixture.path)}
    with Trace(tmp_path / "trace.duckdb") as trace:
        out = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-bench-1")
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(out, spec.output_class)
    found = out.model_dump()
    status = {candidate["name"].lower(): candidate["status"] for candidate in found["candidates"]}
    assert status.get("pymupdf") == "ruled_out", found["candidates"]
    assert found["table"], found
    for cell in found["table"]:
        assert not cell["fact"] or cell["citations"], cell

    assert len(rows) == 1
    agent_ref, pipeline_run_id, cost_usd, error = rows[0]
    assert agent_ref == "benchmark@0.1.0"
    assert pipeline_run_id == "live-bench-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0
