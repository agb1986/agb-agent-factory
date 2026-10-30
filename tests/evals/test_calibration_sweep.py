"""Tests for evals/calibration/sweep.py: deciding labelled diffs at each threshold, and choosing one."""

from pathlib import Path

import pytest

from evals.calibration.sweep import (
    DEFAULT_THRESHOLDS,
    SweepError,
    Triaged,
    check_thresholds,
    render_table,
    sweep,
)
from factory.exit import ExitCode, exit_code
from registry import JevOutput, JevSpec, load_spec
from runtime.gates import with_skip_threshold

TRIAGE = Path(__file__).parents[2] / "agents" / "diff-triage" / "agent.yaml"


@pytest.fixture(scope="module")
def spec() -> JevSpec:
    loaded = load_spec(TRIAGE)
    assert isinstance(loaded, JevSpec)
    return loaded


def _output(spec: JevSpec, p: float, confidence: float = 0.9) -> JevOutput:
    rest = (1 - confidence) / 3
    return spec.output_class.model_validate(
        {
            "needs_review": {"p": p},
            "reviewers": {
                "answer": "code",
                "probs": {
                    "code": confidence,
                    "code-security": rest,
                    "code-perf": rest,
                    "all": rest,
                },
                "confidence": confidence,
            },
        }
    )


def _diff(
    spec: JevSpec,
    id: str,
    risky: bool,
    p: float | None,
    *,
    truncated: bool = False,
    confidence: float = 0.9,
    cost: float = 0.001,
) -> Triaged:
    output = None if p is None else _output(spec, p, confidence)
    return Triaged(id=id, risky=risky, output=output, truncated=truncated, cost_usd=cost)


def _set(spec: JevSpec) -> list[Triaged]:
    return [
        _diff(spec, "r#1", True, 0.9),
        _diff(spec, "r#2", True, 0.12),
        _diff(spec, "s#1", False, 0.01),
        _diff(spec, "s#2", False, 0.04),
        _diff(spec, "s#3", False, 0.2),
        _diff(spec, "s#4", False, 0.01, truncated=True),
    ]


def test_each_threshold_counts_recall_and_skip_rate(spec: JevSpec) -> None:
    result = sweep(spec, _set(spec), [0.05, 0.15, 0.3], question="needs_review")
    by_threshold = {row.threshold: row for row in result.rows}

    assert (by_threshold[0.05].recall_risky, by_threshold[0.05].skip_rate_safe) == (1.0, 0.5)
    assert by_threshold[0.15].missed == ("r#2",)
    assert by_threshold[0.15].recall_risky == 0.5
    assert by_threshold[0.3].skip_rate_safe == 0.75
    assert result.chosen is by_threshold[0.05]
    assert result.question == "needs_review"
    assert result.truncated == 1
    assert result.skip_ceiling_safe == 0.75


def test_a_truncated_diff_is_never_skipped_at_any_threshold(spec: JevSpec) -> None:
    diffs = [
        _diff(spec, "r#1", True, 0.001, truncated=True),
        _diff(spec, "s#1", False, 0.001, truncated=True),
    ]
    result = sweep(spec, diffs, [1.0], question="needs_review")
    (row,) = result.rows
    assert (row.risky_reviewed, row.safe_skipped) == (1, 0)
    assert result.skip_ceiling_safe == 0.0


def test_an_escalating_gate_still_clears_the_skip(spec: JevSpec) -> None:
    # Without `unless`, an escalation clears the skip, so the sweep counts the diff as reviewed.
    data = spec.model_dump()
    for gate in data["gates"]:
        gate["unless"] = None
    plain = JevSpec.model_validate(data)
    diffs = [_diff(plain, "r#1", True, 0.9), _diff(plain, "s#1", False, 0.01, confidence=0.4)]
    (row,) = sweep(plain, diffs, [0.5], question="needs_review").rows
    assert row.safe_skipped == 0


def test_a_gate_with_unless_leaves_the_skip_standing(spec: JevSpec) -> None:
    # The manifest's `unsure` gate yields to `low_risk`, so the low-risk diff is skipped.
    diffs = [_diff(spec, "r#1", True, 0.9), _diff(spec, "s#1", False, 0.01, confidence=0.4)]
    (row,) = sweep(spec, diffs, [0.5], question="needs_review").rows
    assert row.safe_skipped == 1


def test_no_threshold_reaching_the_recall_target_chooses_none(spec: JevSpec) -> None:
    diffs = [_diff(spec, "r#1", True, 0.01), _diff(spec, "s#1", False, 0.01)]
    result = sweep(spec, diffs, [0.05, 0.1], question="needs_review")
    assert result.chosen is None
    assert result.to_json()["chosen"] is None


def test_a_tie_goes_to_the_lower_threshold(spec: JevSpec) -> None:
    diffs = [_diff(spec, "r#1", True, 0.9), _diff(spec, "s#1", False, 0.01)]
    result = sweep(spec, diffs, [0.4, 0.05, 0.2], question="needs_review")
    assert [row.threshold for row in result.rows] == [0.05, 0.2, 0.4]
    assert result.chosen is not None and result.chosen.threshold == 0.05


def test_failed_diffs_are_left_out_and_listed(spec: JevSpec) -> None:
    diffs = [*_set(spec), _diff(spec, "r#3", True, None)]
    result = sweep(spec, diffs, [0.05], question="needs_review")
    assert result.failed == ("r#3",)
    assert result.diffs == 6
    assert result.rows[0].risky == 2
    assert "failed, not measured: r#3" in render_table(result)


def test_cost_per_diff_adds_the_panel_for_the_share_still_reviewed(spec: JevSpec) -> None:
    diffs = [
        _diff(spec, "r#1", True, 0.9, cost=0.002),
        _diff(spec, "s#1", False, 0.01, cost=0.004),
    ]
    alone = sweep(spec, diffs, [0.05], question="needs_review")
    with_panel = sweep(spec, diffs, [0.05], question="needs_review", panel_cost_usd=0.5)

    assert alone.rows[0].cost_per_diff_usd == pytest.approx(0.003)
    assert alone.to_json()["cost_includes_panel"] is False
    assert with_panel.rows[0].cost_per_diff_usd == pytest.approx(0.003 + 0.5 * 0.5)
    assert with_panel.to_json()["cost_includes_panel"] is True


def test_the_question_defaults_to_the_manifests_skip_gate(spec: JevSpec) -> None:
    gated = with_skip_threshold(spec, 0.3, question="reviewers", option="code")
    diffs = [_diff(spec, "r#1", True, 0.9), _diff(spec, "s#1", False, 0.9)]
    result = sweep(gated, diffs, [0.05])
    assert result.question == "reviewers=code"


def test_no_skip_gate_and_no_question_is_a_usage_error(spec: JevSpec) -> None:
    data = spec.model_dump()
    data["gates"] = [{**gate, "unless": None} for gate in data["gates"] if gate["action"] != "skip"]
    ungated = JevSpec.model_validate(data)
    with pytest.raises(SweepError, match="no skip gate") as info:
        sweep(ungated, _set(spec), [0.05])
    assert exit_code(info.value) == ExitCode.USAGE


def test_a_manifest_without_a_recall_target_is_rejected(spec: JevSpec) -> None:
    data = spec.model_dump()
    data["evals"] = {**data["evals"], "min_recall_risky": None}
    bare = JevSpec.model_validate(data)
    with pytest.raises(SweepError, match="min_recall_risky"):
        sweep(bare, _set(spec), [0.05], question="needs_review")


@pytest.mark.parametrize("missing", [True, False])
def test_a_class_with_no_measured_diff_is_rejected(spec: JevSpec, missing: bool) -> None:
    diffs = [d for d in _set(spec) if d.risky != missing]
    with pytest.raises(SweepError, match="risky" if missing else "safe"):
        sweep(spec, diffs, [0.05], question="needs_review")


@pytest.mark.parametrize("thresholds", [[], [0.0], [1.5], [-0.1, 0.2]])
def test_thresholds_outside_zero_to_one_are_rejected(thresholds: list[float]) -> None:
    with pytest.raises(SweepError):
        check_thresholds(thresholds)


def test_thresholds_are_sorted_and_deduplicated() -> None:
    assert check_thresholds([0.3, 0.1, 0.3, 1.0]) == (0.1, 0.3, 1.0)
    assert check_thresholds(DEFAULT_THRESHOLDS) == DEFAULT_THRESHOLDS


def test_the_table_marks_the_chosen_row_and_the_json_carries_every_row(spec: JevSpec) -> None:
    result = sweep(spec, _set(spec), [0.05, 0.15], question="needs_review")
    table = render_table(result)
    chosen = [line for line in table.splitlines() if line.startswith("*")]
    assert len(chosen) == 1 and "0.05" in chosen[0]
    assert "r#2" in table
    assert "no --panel-cost-usd given" in table

    data = result.to_json()
    assert data["chosen"] == 0.05
    assert [row["threshold"] for row in data["rows"]] == [0.05, 0.15]
    assert data["rows"][1]["missed"] == ["r#2"]
    assert data["min_recall_risky"] == 0.98
