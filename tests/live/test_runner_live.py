"""A live smoke test of runtime/runner.py against the real Messages API.

Skipped unless `ANTHROPIC_API_KEY` is set. It makes two small requests to the model below.
"""

from pathlib import Path

import anthropic
import pytest

from registry.resolver import load_spec
from registry.spec import LLMSpec
from runtime.runner import run
from runtime.trace import Trace

MODEL = "claude-sonnet-5-5"

SCHEMAS = """\
from pydantic import BaseModel


class In(BaseModel):
    diff: str


class Out(BaseModel):
    ok: bool
"""

MANIFEST = f"""\
name: live-reviewer
version: 1.0.0
kind: llm
description: Live smoke test agent.
model: {MODEL}
effort: low
system_prompt: system.md
schemas: schemas.py
input_model: In
output_model: Out
tools:
  allow: []
  deny: []
limits:
  max_tool_turns: 1
  timeout_s: 120
  max_output_tokens: 4096
evals:
  min_pass_rate: 0.75
"""

# The prompt has to be longer than the model's minimum cacheable prefix, so the rules repeat.
RULES = "\n".join(
    f"{n}. Check rule {n}: the change keeps names, types and error handling consistent with the "
    f"surrounding code, and no line in hunk {n} introduces a secret, a debug print or dead code."
    for n in range(1, 301)
)
SYSTEM_PROMPT = (
    "You review one unified diff. Answer with ok true when the diff is safe to merge and ok "
    f"false otherwise.\n\n{RULES}\n"
)


@pytest.fixture
def spec(tmp_path: Path) -> LLMSpec:
    directory = tmp_path / "agents" / "live-reviewer"
    directory.mkdir(parents=True)
    (directory / "agent.yaml").write_text(MANIFEST)
    (directory / "system.md").write_text(SYSTEM_PROMPT)
    (directory / "schemas.py").write_text(SCHEMAS)
    spec = load_spec(directory / "agent.yaml")
    assert isinstance(spec, LLMSpec)
    return spec


@pytest.mark.live
def test_two_runs_validate_and_the_second_reads_the_cache(spec: LLMSpec, tmp_path: Path) -> None:
    with anthropic.Anthropic() as client:
        assert MODEL in {model.id for model in client.models.list()}

    payload = {"diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"}
    with Trace(tmp_path / "trace.duckdb") as trace:
        first = run(spec, payload, trace, pipeline_run_id="live-smoke")
        second = run(spec, payload, trace, pipeline_run_id="live-smoke")
        rows = (
            trace._connect()
            .execute(
                "SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "cost_usd, error FROM traces ORDER BY started_at"
            )
            .fetchall()
        )

    assert isinstance(first, spec.output_class)
    assert isinstance(second, spec.output_class)
    assert len(rows) == 2
    for input_tokens, output_tokens, _, _, cost, error in rows:
        assert error is None
        assert input_tokens > 0
        assert output_tokens > 0
        assert cost > 0
    first_read, first_write = rows[0][2], rows[0][3]
    assert first_read + first_write > 0
    assert rows[1][2] > 0
