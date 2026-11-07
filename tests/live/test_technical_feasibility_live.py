"""A live end-to-end run of the technical-feasibility agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It answers the `write-toml-stdlib-only` eval case
against the built `mixed` fixture, which makes at least one request to the manifest's model at the
manifest's effort and fetches the tomllib page from docs.python.org. It checks that the verdict is
`not_viable`, that every cited quote is on its page, and that the run is traced with a cost.
"""

import shutil
from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from evals.graders.citations import check_citations, web_fetcher
from fixtures.build import build
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec, ToolsSpec
from runtime.runner import build_request, run
from runtime.trace import Trace
from tools import web


@pytest.mark.live
def test_writing_toml_with_the_stdlib_alone_is_not_viable(tmp_path: Path) -> None:
    if shutil.which("agb") is None or shutil.which("defuddle") is None:
        pytest.skip("web.fetch needs agb and defuddle on PATH")
    spec = resolve("technical-feasibility", "^0.1")
    assert isinstance(spec, LLMSpec)
    # A missing binary ends a run as soon as the model calls its tool, so rg.search is left out
    # when rg is not on PATH; the agent works with git.read_file and the web tools alone.
    if shutil.which("rg") is None:
        allowed = tuple(name for name in spec.tools.allow if name != "rg.search")
        tools = ToolsSpec(allow=allowed, deny=("rg.search",), web=spec.tools.web)
        spec = spec.model_copy(update={"tools": tools})
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "technical-feasibility" / "evals" / "cases.yaml")
        if case.id == "write-toml-stdlib-only"
    ]
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    payload = dict(case.payload) | {"repo_path": str(fixture.path)}
    web.reset_cache()
    with Trace(tmp_path / "trace.duckdb") as trace:
        answer = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-tf-1")
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(answer, spec.output_class)
    found = answer.model_dump(mode="json")
    assert found["verdict"] == "not_viable", found
    assert case.expected.citations_resolve is not None
    assert check_citations(case.expected.citations_resolve, found, web_fetcher(fixture.path)) == ()

    assert len(rows) == 1
    agent_ref, pipeline_run_id, cost_usd, error = rows[0]
    assert agent_ref == "technical-feasibility@0.1.0"
    assert pipeline_run_id == "live-tf-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0
    print(f"technical-feasibility live run cost: ${cost_usd:.4f}")
