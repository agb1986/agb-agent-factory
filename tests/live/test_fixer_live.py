"""A live end-to-end run of the fixer agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set, and also without Docker and the sandbox image, since the
agent runs the tests there. It fixes the `off-by-one-red` fixture, whose head fails one test on
purpose, on a private copy, then checks with the `sandbox_tests` grader that the full suite passes,
that production code changed and the tests did not, and that the run is traced with a cost.
"""

import shutil
import subprocess
from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
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
def test_the_agent_fixes_the_planted_defect_without_touching_the_tests(tmp_path: Path) -> None:
    spec = resolve("fixer", "^0.1")
    assert isinstance(spec, LLMSpec)
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    case = next(
        c
        for c in load_cases(AGENTS_DIR / "fixer" / "evals" / "cases.yaml")
        if c.id == "off-by-one-red"
    )
    assert case.expected.run is not None
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    assert check_run(case.expected.run, {}, fixture.path), "the fixture should start red"

    payload = {"repo_path": str(fixture.path), **case.payload}
    with Trace(tmp_path / "trace.duckdb") as trace:
        answer = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-fixer")
        rows = (
            trace._connect()
            .execute("SELECT pipeline_run_id, cost_usd, error FROM traces")
            .fetchall()
        )

    assert isinstance(answer, spec.output_class)
    found = answer.model_dump()
    assert found["suite_passes"] is True, found
    assert found["nothing_to_fix"] is False, found
    assert check_run(case.expected.run, found, fixture.path) == []

    assert len(rows) == 1
    assert rows[0][0] == "live-fixer"
    assert rows[0][1] is not None and rows[0][1] > 0
    assert rows[0][2] is None
