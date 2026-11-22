"""Live run of the code-archaeology agent against the real Messages API.

Selected only with `FACTORY_LIVE=1` and skipped unless `ANTHROPIC_API_KEY` is set. It checks that
the manifest's model is offered at the manifest's effort and runs the agent once on the
`billing-run-and-its-hooks` eval case, against the built `legacy-billing` fixture. It checks that
every module the answer maps exists and that the run is traced with a cost. Whether the answer
agrees with the ground truth is for the eval to say, so a case the model fails is reported in the
test's output and fails nothing here.
"""

from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from evals.graders.ground_truth import check_ground_truth, load_truth
from fixtures.build import build
from registry.decomposition_schemas import CodeArchaeologyOutput
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request, run
from runtime.trace import Trace
from tests.live.test_review_panel_live import _without_rg

CASE_ID = "billing-run-and-its-hooks"


@pytest.mark.live
def test_the_agent_maps_the_codebase_with_the_manifest_model_and_effort(tmp_path: Path) -> None:
    spec = resolve("code-archaeology", "^0.1")
    assert isinstance(spec, LLMSpec)
    spec = _without_rg(spec)
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "code-archaeology" / "evals" / "cases.yaml")
        if case.id == CASE_ID
    ]
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    with Trace(tmp_path / "trace.duckdb") as trace:
        output = run(
            spec,
            dict(case.payload) | {"repo_path": str(fixture.path)},
            trace,
            repo_path=fixture.path,
            pipeline_run_id="live-code-archaeology-1",
        )
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces "
                "WHERE kind = 'llm' ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(output, CodeArchaeologyOutput)
    assert output.modules
    for module in output.modules:
        assert (fixture.path / module.path).is_file(), module.path
    assert output.execution_paths
    assert len(rows) == 1
    agent_ref, pipeline_run_id, cost_usd, error = rows[0]
    assert agent_ref == "code-archaeology@0.1.0"
    assert pipeline_run_id == "live-code-archaeology-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0

    expected = case.expected.ground_truth
    assert expected is not None
    answer = output.model_dump(mode="json")
    for failure in check_ground_truth(expected, answer, load_truth(case.fixture)):
        print(f"ground truth: {failure}")
