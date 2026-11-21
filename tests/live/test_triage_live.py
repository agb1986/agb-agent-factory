"""A live triage of the `docs-only` fixture through the review panel, against the real Jev API.

Skipped unless `TYPESAFE_API_KEY` is set. The reviewers run on a fake runner, so no Anthropic key or
spend is involved; only the Jev calls are real, triage's and the judge gate's (#19), and the checks
read triage's rows alone. It checks what holds whatever the model answers: the decision is logged
in a trace row under the pipeline run id, every reviewer still runs because this caller does not
let triage act (only `factory review` does, #20), the Jev cost is in the pipeline total, and `skip` agrees with the `diff-triage` skip gate
(#20): true only when `needs_review` is below the gate's value and nothing escalates.
The pinned model id is checked through the response's `model` field in the Jev run's trace row,
because `GET /v1/models` lists only the aliases (docs/CONVENTIONS.md).
"""

import json
from pathlib import Path

import pytest

from fixtures.build import build
from pipelines.review_panel import REVIEWERS, resolve_panel, run_panel
from runtime.gates import skip_gate
from runtime.jev_transport import available_models
from runtime.trace import Trace
from tests.pipelines.test_review_panel import FakeRunner

RUN_ID = "live-triage-1"


@pytest.mark.live_jev
def test_the_docs_only_diff_is_triaged_logged_and_still_reviewed(tmp_path: Path) -> None:
    panel = resolve_panel(allow_unpublished=True)
    assert panel.triage is not None
    assert "jev-latest" in available_models()

    (fixture,) = build(["docs-only"], dest=tmp_path / "repos")
    runner = FakeRunner()
    with Trace(tmp_path / "trace.duckdb") as trace:
        result = run_panel(
            panel, fixture.path, "base", "head", trace, runner=runner, pipeline_run_id=RUN_ID
        )
        rows = (
            trace._connect()
            .execute(
                "SELECT tag, pipeline_run_id, cost_usd, error, decision_json FROM traces "
                "WHERE kind = 'jev' AND agent_ref = ? ORDER BY started_at",
                [panel.triage.address],
            )
            .fetchall()
        )
        jev_cost = trace.cost_by_agent().get(panel.triage.address, 0.0)

    triage = result.triage
    assert triage.status == "ok", triage.error
    assert triage.decision is not None
    assert triage.probabilities is not None
    assert 0.0 <= triage.probabilities["needs_review"]["p"] <= 1.0

    # one row from the Jev run and one that logs the decision, both under the pipeline run id
    assert {row[1] for row in rows} == {RUN_ID}
    assert [row[0] for row in rows].count("triage:decision") == 1
    assert all(row[3] is None for row in rows)
    (jev_run,) = [row for row in rows if row[0] != "triage:decision"]
    assert json.loads(jev_run[4])["model"] == panel.triage.model

    # the manifest lets triage act, but this caller does not, so the run is route only and every
    # reviewer ran
    assert runner.reviewers_called() == {name for name, _ in REVIEWERS}
    document = result.to_json()["triage"]
    assert (document["route_only"], document["may_act"], document["outcome"]) == (
        True,
        False,
        "full",
    )

    # the gates in the manifest, not the model, decide skip; an escalation or a cut input clears it
    gate = skip_gate(panel.triage)
    assert gate is not None and gate.rule == "p_below"
    below = triage.probabilities["needs_review"]["p"] < gate.value
    assert triage.decision.skip == (below and not triage.decision.escalate and not triage.truncated)
    assert jev_cost > 0
    assert result.total_cost_usd >= jev_cost
