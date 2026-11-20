"""Live runs of the triage-diagnostics agent and the incident pipeline against the real Messages API.

Selected only with `FACTORY_LIVE=1` and skipped unless `ANTHROPIC_API_KEY` is set. The first test
checks that the manifest's model is offered at the manifest's effort and runs the agent once on
the `empty-ratings-divide-by-zero` eval case, against the built `crash-empty-average` fixture at
`head`, which plays the release tag. It checks that the suspected paths name the planted fault,
that every suspected path exists and that the run is traced with a cost.

The second test also needs Docker and the sandbox image. It runs `run_incident` end to end on the
same event: the agent, then test-writer in reproduce mode in a clone, then the reproduction run in
the offline sandbox. It checks that the ticket is filed with kind `incident`, that the body has
the documented sections and that both runs are traced under one pipeline run id with a cost. It
does not require a reproduction, because a model may miss it; it reports which way it went.
The agents are resolved with `allow_unpublished`, since the test runs before they are published.
"""

import shutil
import subprocess
from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from fixtures.build import build
from pipelines.incident import (
    BODY_SECTIONS,
    incident_sections,
    parse_event,
    resolve_incident_agents,
    run_incident,
)
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request, run
from runtime.trace import Trace
from tests.live.test_review_panel_live import _without_rg
from tools.sandbox import sandbox_image

CASE_ID = "empty-ratings-divide-by-zero"


def _case() -> tuple[dict, str]:
    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "triage-diagnostics" / "evals" / "cases.yaml")
        if case.id == CASE_ID
    ]
    assert case.expected.incident is not None
    return dict(case.payload), case.expected.incident.suspected_path


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", sandbox_image()], capture_output=True, check=False
    )
    return proc.returncode == 0


def _rows(trace: Trace) -> list[tuple]:
    return (
        trace._connect()
        .execute(
            "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces "
            "WHERE kind = 'llm' ORDER BY started_at"
        )
        .fetchall()
    )


@pytest.mark.live
def test_the_agent_names_the_planted_fault_with_the_manifest_model_and_effort(
    tmp_path: Path,
) -> None:
    spec = resolve("triage-diagnostics", "^0.1")
    assert isinstance(spec, LLMSpec)
    spec = _without_rg(spec)
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"

    payload, suspected = _case()
    (fixture,) = build(["crash-empty-average"], dest=tmp_path / "repos")
    with Trace(tmp_path / "trace.duckdb") as trace:
        output = run(
            spec,
            payload | {"repo_path": str(fixture.path)},
            trace,
            repo_path=fixture.path,
            pipeline_run_id="live-triage-diagnostics-1",
        )
        rows = _rows(trace)

    assert isinstance(output, spec.output_class)
    document = output.model_dump()
    assert suspected in document["suspected_paths"], document
    for path in document["suspected_paths"]:
        assert (fixture.path / path).is_file(), path
    assert len(rows) == 1
    agent_ref, pipeline_run_id, cost_usd, error = rows[0]
    assert agent_ref == "triage-diagnostics@0.1.0"
    assert pipeline_run_id == "live-triage-diagnostics-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0


@pytest.mark.live
@pytest.mark.skipif(not _image_present(), reason=f"docker image {sandbox_image()} absent")
def test_the_pipeline_files_an_incident_traced_under_one_run(tmp_path: Path) -> None:
    agents = resolve_incident_agents(allow_unpublished=True)
    agents = type(agents)(
        triage=_without_rg(agents.triage), test_writer=_without_rg(agents.test_writer)
    )
    payload, _ = _case()
    (fixture,) = build(["crash-empty-average"], dest=tmp_path / "repos")
    work = tmp_path / "work"
    work.mkdir()
    with Trace(tmp_path / "trace.duckdb") as trace:
        event = parse_event(agents.triage, payload["event"])
        result = run_incident(
            agents,
            fixture.path,
            event,
            trace,
            out_dir=tmp_path / "out",
            work_dir=work,
            pipeline_run_id="live-incident-1",
        )
        rows = _rows(trace)

    assert result.item.kind == "incident"
    assert result.item.status == ("ready" if result.reproduction.reproduced else "todo")
    assert tuple(incident_sections(result.item.body)) == BODY_SECTIONS
    assert result.report_path.is_file()
    refs = [row[0] for row in rows]
    assert refs[0] == agents.triage.address and agents.test_writer.address in refs
    assert {row[1] for row in rows} == {"live-incident-1"}
    assert all(row[3] is None for row in rows), rows
    assert result.total_cost_usd > 0
    print(f"reproduced={result.reproduction.reproduced} why_not={result.reproduction.why_not}")
