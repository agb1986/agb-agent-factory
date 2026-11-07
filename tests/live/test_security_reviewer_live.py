"""A live end-to-end run of the security-reviewer agent against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It reviews the `sql-injection` fixture once, which makes
one request series to the manifest's model, and checks that the planted defect is found as a
blocking finding with a category from the prompt's fixed list, and that the run is traced with a
cost.
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
CATEGORIES = {
    "sql-injection",
    "command-injection",
    "path-traversal",
    "missing-authorization",
    "hardcoded-secret",
    "unsafe-deserialization",
    "vulnerable-dependency",
    "weak-randomness",
    "other-security",
}


@pytest.mark.live
def test_review_finds_the_planted_injection_and_is_traced(tmp_path: Path) -> None:
    spec = resolve("security-reviewer", "^1.0")
    assert isinstance(spec, LLMSpec)
    # A missing `rg` or `bandit` ends a run as soon as the model calls the tool that needs it, so
    # without one the agent runs with the rest, which is a supported configuration.
    missing = tuple(
        tool for tool, binary in (("rg.search", "rg"), ("sast.scan", "bandit"))
        if shutil.which(binary) is None
    )  # fmt: skip
    if missing:
        allowed = tuple(name for name in spec.tools.allow if name not in missing)
        spec = spec.model_copy(update={"tools": ToolsSpec(allow=allowed, deny=missing)})
    with anthropic.Anthropic() as client:
        assert spec.model in {model.id for model in client.models.list()}

    (fixture,) = build(["sql-injection"], dest=tmp_path / "repos")
    payload = {"repo_path": str(fixture.path), "base": "base", "head": "head"}
    with Trace(tmp_path / "trace.duckdb") as trace:
        review = run(
            spec, payload, trace, repo_path=fixture.path, pipeline_run_id="live-security-1"
        )
        rows = (
            trace._connect()
            .execute(
                "SELECT run_id, pipeline_run_id, input_tokens, output_tokens, cost_usd, error "
                "FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(review, spec.output_class)
    found = review.model_dump()
    assert any(
        finding["file"] == "accounts/db.py"
        and finding["severity"] in BLOCKING
        and finding["line_start"] <= 31
        and finding["line_end"] >= 30
        for finding in found["findings"]
    ), found
    assert found["verdict"] == "request_changes"
    assert all(finding["category"] in CATEGORIES for finding in found["findings"]), found

    assert len(rows) == 1
    assert rows[0][1] == "live-security-1"
    assert rows[0][5] is None
    assert rows[0][4] is not None and rows[0][4] > 0
