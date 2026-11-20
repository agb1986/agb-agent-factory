"""A live end-to-end run of the feature-coder agent against the real Messages API.

Skipped unless `FACTORY_LIVE` is set with `ANTHROPIC_API_KEY`, and also without Docker and the
sandbox image, since the agent runs the tests there. It first checks that the manifest's model
exists, which is how `claude-sonnet-5-5` at effort `xhigh` was chosen and is kept honest. It then
builds the `reserve-per-order-limit` case on a private copy of the `partly-covered` fixture and
checks the work tree with the `sandbox_tests` grader (the suite passes, the listed tests exist and
pass, production code changed) and the answer with the rubric's deterministic checks. The rubric's
model-scored criteria are not run here; `factory eval feature-coder` does that.
"""

import shutil
import subprocess
from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from evals.graders.rubric import check_output
from evals.graders.sandbox_tests import check_run
from fixtures.build import build
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import run
from runtime.trace import Trace
from tools.sandbox import SANDBOX_IMAGE


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, check=False
    )
    return proc.returncode == 0


@pytest.mark.live
@pytest.mark.skipif(not _image_present(), reason=f"docker image {SANDBOX_IMAGE} absent")
def test_the_agent_builds_a_ticket_with_a_test_per_criterion(tmp_path: Path) -> None:
    spec = resolve("feature-coder", "^0.1")
    assert isinstance(spec, LLMSpec)
    assert (spec.model, spec.effort) == ("claude-sonnet-5-5", "xhigh")
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    case = next(
        c
        for c in load_cases(AGENTS_DIR / "feature-coder" / "evals" / "cases.yaml")
        if c.id == "reserve-per-order-limit"
    )
    assert case.expected.run is not None and case.expected.rubric is not None
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")

    payload = {"repo_path": str(fixture.path), **case.payload}
    with Trace(tmp_path / "trace.duckdb") as trace:
        answer = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-feature")
        rows = (
            trace._connect()
            .execute("SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces")
            .fetchall()
        )

    assert isinstance(answer, spec.output_class)
    found = answer.model_dump()
    assert found["blocked"] is False, found
    assert len(found["acceptance"]) == len(case.payload["ticket"]["acceptance"]), found
    assert all(check["tests"] for check in found["acceptance"]), found
    assert check_run(case.expected.run, found, fixture.path) == []
    assert check_output(case.expected.rubric, found, fixture.path) == ()

    agent_rows = [row for row in rows if row[0] == spec.address]
    assert len(agent_rows) == 1
    for _, pipeline_run_id, cost_usd, error in rows:
        assert pipeline_run_id == "live-feature"
        assert cost_usd is not None and cost_usd > 0
        assert error is None
