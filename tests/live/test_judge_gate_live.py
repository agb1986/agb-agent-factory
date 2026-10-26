"""A live judge-gate run through the review panel, against the real Jev API.

Skipped unless `TYPESAFE_API_KEY` is set. The reviewers and the judge run on a fake runner, so no
Anthropic key or spend is involved; only the triage and judge-gate calls are real. Two panels are
run, each on its own trace store:

- a clean refactor that every reviewer approves with no findings, where the gate is expected to be
  sure and skip the judge;
- a split panel, where one reviewer blocks on a major finding and the rest approve, where the gate
  is expected to hand the change to the judge.

The pinned model id is not checked against the models listing, which names only the rolling
aliases; the call itself fails if the id is rejected. What the model answers is not asserted beyond those two outcomes and what must hold either way: the
decision is in a `judge_gate:decision` trace row under the pipeline run id, the skip agrees with the
manifest's gates, and a skipped judge leaves a cost estimate and a lower pipeline total.
"""

from pathlib import Path
from typing import Any

import pytest

from fixtures.build import build
from pipelines.review_panel import (
    JUDGE_GATE_TAG,
    PATH_ESCALATED,
    PATH_SKIPPED,
    resolve_panel,
    run_panel,
)
from registry.common_schemas import ReviewOutput
from runtime.trace import Trace
from tests.pipelines.test_review_panel import FakeRunner, _review


class CleanRunner(FakeRunner):
    """A fake runner whose reviewers all approve with no findings."""

    def __call__(self, spec: Any, payload: Any, trace: Trace, **kwargs: Any) -> Any:
        output = super().__call__(spec, payload, trace, **kwargs)
        if spec.name == "judge":
            return output
        return ReviewOutput.model_validate(_review())


def _gate_rows(trace: Trace, run_id: str) -> list[tuple[Any, ...]]:
    return (
        trace._connect()
        .execute(
            "SELECT tag, pipeline_run_id, cost_usd, error FROM traces "
            "WHERE tag = ? AND pipeline_run_id = ?",
            [JUDGE_GATE_TAG, run_id],
        )
        .fetchall()
    )


@pytest.mark.live_jev
def test_a_clean_refactor_skips_the_judge_and_a_split_panel_escalates(tmp_path: Path) -> None:
    panel = resolve_panel(allow_unpublished=True)
    assert panel.gate is not None

    clean, split = build(["clean-refactor", "mixed"], dest=tmp_path / "repos")
    clean_runner = CleanRunner()
    with Trace(tmp_path / "clean.duckdb") as trace:
        skipped = run_panel(
            panel,
            clean.path,
            "base",
            "head",
            trace,
            runner=clean_runner,
            pipeline_run_id="live-gate-clean",
        )
        clean_rows = _gate_rows(trace, "live-gate-clean")
    split_runner = FakeRunner()
    with Trace(tmp_path / "split.duckdb") as trace:
        escalated = run_panel(
            panel,
            split.path,
            "base",
            "head",
            trace,
            runner=split_runner,
            pipeline_run_id="live-gate-split",
        )
        split_rows = _gate_rows(trace, "live-gate-split")

    # the clean refactor: sure, so the judge never ran and the saving is on the record
    assert skipped.gate.status == "ok", skipped.gate.error
    assert skipped.gate.skipped
    assert skipped.gate.path == PATH_SKIPPED
    assert skipped.gate.verdict == "ship"
    assert skipped.ship is True
    assert skipped.judgement is None
    assert not any(call["spec"].name == "judge" for call in clean_runner.calls)
    assert skipped.gate.judge_cost_estimate_usd is not None
    assert skipped.gate.judge_cost_estimate_usd > 0
    assert len(clean_rows) == 1
    assert clean_rows[0][3] is None

    # the split panel: not sure, so the judge ran and its score decides
    assert escalated.gate.status == "ok", escalated.gate.error
    assert not escalated.gate.skipped
    assert escalated.gate.path == PATH_ESCALATED
    assert escalated.judgement is not None
    assert any(call["spec"].name == "judge" for call in split_runner.calls)
    assert len(split_rows) == 1

    # the gates in the manifest, not the model, decide skip
    assert escalated.gate.decision is not None
    assert escalated.gate.decision.skip is False
    assert skipped.gate.decision is not None
    assert skipped.gate.decision.reason == "judge_gate"
