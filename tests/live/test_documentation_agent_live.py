"""A live end-to-end run of the documentation agent against the real Messages API.

Skipped unless `FACTORY_LIVE` is set with `ANTHROPIC_API_KEY`, and also without Docker and the
sandbox image, since the agent runs the suite there. It first checks that the manifest's model
exists, which is how `claude-sonnet-5-5` at effort `xhigh` was chosen and is kept honest. It then
runs the `document-new-function` case on a private copy of the `docs-new-function` fixture and
checks the work tree with the `sandbox_tests` grader's deterministic part: the suite passes, no
test file changed, and the drift check finds nothing (`slugify` has a docstring and is named in a
docs file, and no code changed outside docstrings and comments). The case's judged criteria are
not run here; `factory eval documentation-agent` does that. Each trace row must carry this run's
id and a cost.
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
def test_the_agent_documents_a_new_function_without_changing_code(tmp_path: Path) -> None:
    spec = resolve("documentation-agent", "^0.1")
    assert isinstance(spec, LLMSpec)
    assert (spec.model, spec.effort) == ("claude-sonnet-5-5", "xhigh")
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    case = next(
        c
        for c in load_cases(AGENTS_DIR / "documentation-agent" / "evals" / "cases.yaml")
        if c.id == "document-new-function"
    )
    assert case.expected.run is not None and case.expected.run.doc_drift is not None
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")

    payload = {"repo_path": str(fixture.path), **case.payload}
    with Trace(tmp_path / "trace.duckdb") as trace:
        answer = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-docs")
        rows = (
            trace._connect()
            .execute("SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces")
            .fetchall()
        )

    assert isinstance(answer, spec.output_class)
    found = answer.model_dump()
    assert found["blocked"] is False, found
    assert {change["path"] for change in found["docs_changed"]} >= {"README.md"}, found
    assert check_run(case.expected.run, found, fixture.path) == []

    agent_rows = [row for row in rows if row[0] == spec.address]
    assert len(agent_rows) == 1
    for _, pipeline_run_id, cost_usd, error in rows:
        assert pipeline_run_id == "live-docs"
        assert cost_usd is not None and cost_usd > 0
        assert error is None
