"""A live end-to-end run of the judge agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It judges the `severity-disagreement` eval case twice
against the built `off-by-one` fixture, which makes at least two requests to the manifest's model,
and checks that the planted defect survives at the stricter severity, that `runtime.scoring` can
weigh the judgement, that the two runs are traced under distinct run ids with a cost, and that the
second one reads the prompt cache.
"""

from pathlib import Path

import anthropic
import pytest

from evals.cases import load_cases
from fixtures.build import build
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import run
from runtime.scoring import Assessment, assess
from runtime.trace import Trace

BLOCKING = {"major", "blocker"}


@pytest.mark.live
def test_judge_keeps_the_stricter_severity_and_the_second_run_reads_the_cache(
    tmp_path: Path,
) -> None:
    spec = resolve("judge", "^2.0")
    assert isinstance(spec, LLMSpec)
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    (case,) = [
        case
        for case in load_cases(AGENTS_DIR / "judge" / "evals" / "cases.yaml")
        if case.id == "severity-disagreement"
    ]
    (fixture,) = build([case.fixture], dest=tmp_path / "repos")
    payload = dict(case.payload) | {"repo_path": str(fixture.path)}
    with Trace(tmp_path / "trace.duckdb") as trace:
        first = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-judge-1")
        second = run(spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-judge-2")
        rows = (
            trace._connect()
            .execute(
                "SELECT run_id, pipeline_run_id, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    for judgement in (first, second):
        assert isinstance(judgement, spec.output_class)
        found = judgement.model_dump()
        assert any(
            finding["file"] == "series/rolling.py"
            and finding["severity"] in BLOCKING
            and finding["line_start"] <= 13
            and finding["line_end"] >= 12
            for finding in found["merged_findings"]
        ), found
        for finding in found["merged_findings"]:
            assert set(finding["sources"]) <= set(payload["reviews"]), finding
        assessment = assess(spec, judgement)
        assert isinstance(assessment, Assessment)
        assert 0 <= assessment.score <= 1

    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert [row[1] for row in rows] == ["live-judge-1", "live-judge-2"]
    for row in rows:
        assert row[7] is None
        assert row[6] is not None and row[6] > 0
    assert rows[1][4] > 0
