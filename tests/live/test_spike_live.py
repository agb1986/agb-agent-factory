"""A live end-to-end run of the spike agent against the real Messages API and a public API.

Skipped unless `ANTHROPIC_API_KEY` is set. It answers the `jsonplaceholder-nested-resources` eval
case, which makes at least one request to the manifest's model at the manifest's effort, runs code
in the networked spike sandbox, and has every tool call that is not read-only judged by
action-guard 0.2. It checks that the answer reports what the code observed, that every cited quote
is on its page, that the spike run and the guard's decisions are traced with a cost, and that the
scratch directory is gone afterwards.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from evals.graders.citations import check_citations, web_fetcher
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request, run
from runtime.trace import Trace
from tools import web
from tools.sandbox import SANDBOX_IMAGE


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, check=False
    )
    return proc.returncode == 0


@pytest.mark.live
def test_a_spike_reports_the_real_shape_of_jsonplaceholder(tmp_path: Path) -> None:
    if shutil.which("agb") is None or shutil.which("defuddle") is None:
        pytest.skip("web.fetch needs agb and defuddle on PATH")
    if not _image_present():
        pytest.skip(f"the spike sandbox needs the docker image {SANDBOX_IMAGE}")
    spec = resolve("spike", "^0.1")
    assert isinstance(spec, LLMSpec)
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "medium"

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "spike" / "evals" / "cases.yaml")
        if case.id == "jsonplaceholder-nested-resources"
    ]
    before = set(Path(tempfile.gettempdir()).glob("factory-spike-*"))
    web.reset_cache()
    with Trace(tmp_path / "trace.duckdb") as trace:
        answer = run(spec, case.payload, trace, pipeline_run_id="live-spike-1")
        rows = (
            trace._connect()
            .execute(
                "SELECT agent_ref, pipeline_run_id, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )
    assert set(Path(tempfile.gettempdir()).glob("factory-spike-*")) <= before

    assert isinstance(answer, spec.output_class)
    found = answer.model_dump(mode="json")
    assert found["observations"], found
    seen = " ".join(item["output"] for item in found["observations"])
    for key in ("userId", "postId", "404"):
        assert key in seen, key
    assert found["working_code"] is not None and found["working_code"]["content"].strip()
    assert case.expected.citations_resolve is not None
    assert check_citations(case.expected.citations_resolve, found, web_fetcher(tmp_path)) == ()

    spike_rows = [row for row in rows if row[0] == "spike@0.1.0"]
    assert len(spike_rows) == 1
    _, pipeline_run_id, cost_usd, error = spike_rows[0]
    assert pipeline_run_id == "live-spike-1"
    assert error is None
    assert cost_usd is not None and cost_usd > 0
    guard_rows = [row for row in rows if row[0].startswith("action-guard@0.2.")]
    assert guard_rows, "no tool call was put to the guard"
    print(f"spike live run cost: ${cost_usd:.4f}, guard decisions: {len(guard_rows)}")
