"""A live run of `run_jev` against the real TypeSafe API.

Skipped unless `TYPESAFE_API_KEY` is set; the `live_jev` marker is separate from `live`, so having
only an Anthropic key does not run it. It sends the diff-triage questions about a short diff in one
request, and checks that the probability is in range and that the run is traced under the model the
manifest pinned.

`GET /v1/models` lists only the aliases `jev-latest` and `jev-preview`, never a versioned id, so the
pinned id is verified the way docs/CONVENTIONS.md says: the listing holds `jev-latest`, and the
response's `model` field, which the trace row records, equals the pinned id.
"""

import json
from pathlib import Path

import pytest

from registry.jev_spec import JevSpec
from registry.resolver import resolve
from runtime.jev_runner import run_jev
from runtime.jev_transport import available_models
from runtime.trace import Trace

DIFF = """\
--- a/series/rolling.py
+++ b/series/rolling.py
@@ -10,4 +10,4 @@ def window_sum(values, size):
-    for i in range(len(values) - size + 1):
+    for i in range(len(values) - size):
         total.append(sum(values[i : i + size]))
"""


@pytest.mark.live_jev
def test_a_noul_answer_comes_back_in_range_and_is_traced(tmp_path: Path) -> None:
    spec = resolve("diff-triage", "^0.3")
    assert isinstance(spec, JevSpec)
    assert "jev-latest" in available_models()

    with Trace(tmp_path / "trace.duckdb") as trace:
        output = run_jev(
            spec,
            {"files": "series/rolling.py", "diff": DIFF},
            trace,
            pipeline_run_id="live-jev-1",
        )
        (row,) = (
            trace._connect()
            .execute(
                "SELECT kind, pipeline_run_id, input_tokens, output_tokens, cost_usd, "
                "decision_json, error FROM traces"
            )
            .fetchall()
        )

    probability = output.model_dump()["needs_review"]["p"]
    assert 0.0 <= probability <= 1.0
    kind, pipeline_run_id, input_tokens, output_tokens, cost, decision, error = row
    assert (kind, pipeline_run_id, error) == ("jev", "live-jev-1", None)
    assert input_tokens and input_tokens > 0 and output_tokens == 0
    assert cost and cost > 0
    assert json.loads(decision)["model"] == spec.model
