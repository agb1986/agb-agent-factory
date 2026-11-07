"""A live end-to-end run of the test-writer agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set, and also without Docker and the sandbox image, since the
agent runs its tests there. It asks the agent for a test of the `off-by-one` defect on a private
copy of the fixture, then checks with the `sandbox_tests` grader that the new test fails on the
head tree and passes on the base state, that production code is untouched, and that the run is
traced with a cost.
"""

import shutil
import subprocess
from pathlib import Path

import anthropic
import pytest

from evals.cases import ExpectedRun, load_cases
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
def test_the_agent_writes_a_test_that_fails_for_the_planted_defect(tmp_path: Path) -> None:
    spec = resolve("test-writer", "^0.2")
    assert isinstance(spec, LLMSpec)
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    case = next(
        c
        for c in load_cases(AGENTS_DIR / "test-writer" / "evals" / "cases.yaml")
        if c.id == "off-by-one"
    )
    assert case.expected.run is not None
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    payload = {"repo_path": str(fixture.path), **case.payload}
    with Trace(tmp_path / "trace.duckdb") as trace:
        answer = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-tw")
        rows = (
            trace._connect()
            .execute("SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces")
            .fetchall()
        )

    assert isinstance(answer, spec.output_class)
    found = answer.model_dump()
    assert found["test_files"], found
    assert found["fails_on_head"] is True, found
    assert check_run(case.expected.run, found, fixture.path) == []
    assert check_run(ExpectedRun(tests="changed"), found, fixture.path) == []

    # One row for the agent and one per guard decision, all in this pipeline run and all priced.
    agent_rows = [row for row in rows if row[0] == spec.address]
    guard_rows = [row for row in rows if row[0].startswith("action-guard@")]
    assert len(agent_rows) == 1
    assert len(agent_rows) + len(guard_rows) == len(rows)
    assert guard_rows, rows
    for _, pipeline_run_id, cost_usd, error in rows:
        assert pipeline_run_id == "live-tw"
        assert cost_usd is not None and cost_usd > 0
        assert error is None
