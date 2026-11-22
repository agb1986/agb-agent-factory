"""Live run of the extraction-modularization agent against the real Messages API.

Selected only with `FACTORY_LIVE=1` and skipped unless `ANTHROPIC_API_KEY` is set. It checks that
the manifest's model is offered at the manifest's effort and runs the agent once on the
`late-fees-full-map` eval case, against the built `legacy-billing` fixture. It checks that every
symbol the blueprint moves and every test it names is in a file that exists, that the blueprint
makes a ticket it can be read back from, and that the run is traced with a cost. Whether the
boundary agrees with the ground truth is for the eval to say, so a case the model fails is reported
in the test's output and fails nothing here. The judge's rubric is not run.
"""

from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from evals.graders.ground_truth import check_ground_truth, load_truth
from fixtures.build import build
from pipelines.decompose import blueprint_of, extraction_ticket
from registry.decomposition_schemas import ExtractionBlueprint
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request, run
from runtime.trace import Trace
from tests.live.test_review_panel_live import _without_rg

CASE_ID = "late-fees-full-map"


@pytest.mark.live
def test_the_agent_draws_a_boundary_with_the_manifest_model_and_effort(tmp_path: Path) -> None:
    spec = resolve("extraction-modularization", "^0.1")
    assert isinstance(spec, LLMSpec)
    spec = _without_rg(spec)
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "extraction-modularization" / "evals" / "cases.yaml")
        if case.id == CASE_ID
    ]
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    with Trace(tmp_path / "trace.duckdb") as trace:
        output = run(
            spec,
            dict(case.payload) | {"repo_path": str(fixture.path)},
            trace,
            repo_path=fixture.path,
            pipeline_run_id="live-extraction-modularization-1",
        )
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces "
                "WHERE kind = 'llm' ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(output, ExtractionBlueprint)
    assert output.feature == case.payload["feature"]["name"]
    assert output.start == case.payload["feature"]["start"]
    ticket = extraction_ticket(output, repo=str(fixture.path), ref="head")
    for path in (*ticket.impacted_paths, *(test.path for test in output.pinning_tests)):
        assert (fixture.path / path).is_file(), path
    assert blueprint_of(ticket.body) == output
    assert len(rows) == 1
    agent_ref, pipeline_run_id, cost_usd, error = rows[0]
    assert agent_ref == "extraction-modularization@0.1.0"
    assert pipeline_run_id == "live-extraction-modularization-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0

    expected = case.expected.ground_truth
    assert expected is not None
    answer = output.model_dump(mode="json")
    for failure in check_ground_truth(expected, answer, load_truth(case.fixture)):
        print(f"ground truth: {failure}")
