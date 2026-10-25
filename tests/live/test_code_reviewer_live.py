"""A live end-to-end run of the code-reviewer agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It reviews the `off-by-one` fixture twice, which makes
two requests to the manifest's model, and checks that the planted defect is found, that the two
runs are traced under distinct run ids with a cost, and that the second one reads the prompt cache.
"""

import shutil
from pathlib import Path

import anthropic
import pytest

from fixtures.build import build
from registry.resolver import resolve
from registry.spec import LLMSpec, ToolsSpec
from runtime.runner import run
from runtime.trace import Trace

BLOCKING = {"major", "blocker"}


@pytest.mark.live
def test_review_finds_the_planted_defect_and_the_second_run_reads_the_cache(
    tmp_path: Path,
) -> None:
    spec = resolve("code-reviewer", "^1.2")
    assert isinstance(spec, LLMSpec)
    if shutil.which("rg") is None:
        # A missing `rg` ends a run as soon as the model calls `rg.search`, so without it the
        # agent runs with the two git tools, which is a supported configuration.
        allowed = tuple(name for name in spec.tools.allow if name != "rg.search")
        spec = spec.model_copy(update={"tools": ToolsSpec(allow=allowed, deny=("rg.search",))})
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    (fixture,) = build(["off-by-one"], dest=tmp_path / "repos")
    payload = {"repo_path": str(fixture.path), "base": "base", "head": "head"}
    with Trace(tmp_path / "trace.duckdb") as trace:
        first = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-review-1")
        second = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-review-2")
        rows = (
            trace._connect()
            .execute(
                "SELECT run_id, pipeline_run_id, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    for review in (first, second):
        assert isinstance(review, spec.output_class)
        found = review.model_dump()
        assert any(
            finding["file"] == "series/rolling.py"
            and finding["severity"] in BLOCKING
            and finding["line_start"] <= 13
            and finding["line_end"] >= 12
            for finding in found["findings"]
        ), found
        assert found["verdict"] == "request_changes"

    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert [row[1] for row in rows] == ["live-review-1", "live-review-2"]
    for row in rows:
        assert row[7] is None
        assert row[6] is not None and row[6] > 0
    assert rows[1][4] > 0
