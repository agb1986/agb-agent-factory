"""Tests for runtime/gates.py: every gate rule, routing, and the rules that always hold."""

import copy
from pathlib import Path
from typing import Any

import pytest

from registry import JevOutput, JevSpec
from runtime.gates import (
    JUDGE_GATE,
    NO_GATE,
    ROUTE_ONLY,
    SKIP_GATE_NAME,
    TRUNCATED,
    Decision,
    decide,
    decide_judge_gate,
    jev_error,
    skip_gate,
    with_skip_threshold,
)

MANIFEST: dict[str, Any] = {
    "name": "triage",
    "version": "1.0.0",
    "kind": "jev",
    "description": "Test Jev agent.",
    "model": "jev-1.14.0",
    "input": {"render": "Diff:\n{diff}", "max_chars": 1000},
    "questions": [
        {"id": "risky", "type": "noul", "text": "Is it risky?"},
        {"id": "area", "type": "choice", "text": "Which area?", "options": ["ui", "api", "db"]},
        {
            "id": "size",
            "type": "score",
            "text": "How big?",
            "range": [0, 2],
            "levels": ["small", "medium", "large"],
        },
    ],
    "gates": [
        {"name": "calm", "question": "risky", "rule": "p_below", "value": 0.1, "action": "skip"},
        {
            "name": "alarm",
            "question": "risky",
            "rule": "p_above",
            "value": 0.9,
            "action": "escalate",
        },
        {
            "name": "api_likely",
            "question": "area",
            "rule": "p_above",
            "option": "api",
            "value": 0.7,
            "action": "escalate",
        },
        {
            "name": "db_unlikely",
            "question": "area",
            "rule": "p_below",
            "option": "db",
            "value": 0.05,
            "action": "skip",
        },
        {
            "name": "unsure",
            "question": "area",
            "rule": "confidence_below",
            "value": 0.5,
            "action": "escalate",
        },
        {
            "name": "docs_only",
            "question": "area",
            "rule": "answer_in",
            "value": ["ui"],
            "action": "skip",
        },
        {
            "name": "huge",
            "question": "size",
            "rule": "answer_in",
            "value": ["large"],
            "action": "escalate",
        },
    ],
    "routing": [
        {"when": "unsure", "route": ["code-reviewer", "security-reviewer"]},
        {"when": "alarm", "route": ["security-reviewer", "perf-reviewer"]},
        {"when": "area=db", "route": ["perf-reviewer"]},
        {"when": "size=large", "route": ["code-reviewer"]},
    ],
    "evals": {"min_pass_rate": 0.9, "min_recall_risky": 0.98},
}


def _spec(**changes: Any) -> JevSpec:
    data = copy.deepcopy(MANIFEST) | changes
    return JevSpec.model_validate({**data, "path": Path("agent.yaml")})


def _output(
    spec: JevSpec,
    *,
    p: float = 0.5,
    area: str = "ui",
    confidence: float = 0.9,
    size: str = "small",
    area_probs: dict[str, float] | None = None,
) -> JevOutput:
    area_probs = area_probs or {"ui": 0.4, "api": 0.3, "db": 0.3}
    levels = ["small", "medium", "large"]
    return spec.output_class.model_validate(
        {
            "risky": {"p": p},
            "area": {"answer": area, "probs": area_probs, "confidence": confidence},
            "size": {
                "value": float(levels.index(size)),
                "level": size,
                "probs": {level: 1.0 if level == size else 0.0 for level in levels},
                "confidence": 0.9,
            },
        }
    )


@pytest.fixture(scope="module")
def spec() -> JevSpec:
    return _spec()


def test_route_only_is_on_until_calibration() -> None:
    assert ROUTE_ONLY is True


def test_a_decision_is_frozen() -> None:
    decision = Decision(skip=False, escalate=False, route=(), reason=NO_GATE)
    with pytest.raises(AttributeError):
        decision.skip = True  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_to_json_lists_the_route() -> None:
    decision = Decision(skip=False, escalate=True, route=("a", "b"), reason="unsure")
    assert decision.to_json() == {
        "skip": False,
        "escalate": True,
        "route": ["a", "b"],
        "reason": "unsure",
    }


# --- gate rules


def test_p_below_on_a_noul_question_skips(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, p=0.05, area="api"), False)
    assert decision == Decision(skip=True, escalate=False, route=(), reason="calm")


def test_p_below_does_not_fire_at_the_value(spec: JevSpec) -> None:
    assert decide(spec, _output(spec, p=0.1, area="api"), False).skip is False


def test_p_above_on_a_noul_question_escalates(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, p=0.95, area="api"), False)
    assert decision == Decision(
        skip=False, escalate=True, route=("security-reviewer", "perf-reviewer"), reason="alarm"
    )


def test_p_above_does_not_fire_at_the_value(spec: JevSpec) -> None:
    assert decide(spec, _output(spec, p=0.9, area="api"), False).escalate is False


def test_p_above_reads_the_probability_of_the_option(spec: JevSpec) -> None:
    probs = {"ui": 0.1, "api": 0.8, "db": 0.1}
    decision = decide(spec, _output(spec, area="api", area_probs=probs), False)
    assert decision == Decision(skip=False, escalate=True, route=(), reason="api_likely")


def test_p_below_reads_the_probability_of_the_option(spec: JevSpec) -> None:
    probs = {"ui": 0.5, "api": 0.48, "db": 0.02}
    decision = decide(spec, _output(spec, area="api", area_probs=probs), False)
    assert decision == Decision(skip=True, escalate=False, route=(), reason="db_unlikely")


def test_confidence_below_escalates_and_routes(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="api", confidence=0.49), False)
    assert decision == Decision(
        skip=False, escalate=True, route=("code-reviewer", "security-reviewer"), reason="unsure"
    )


def test_confidence_at_the_value_does_not_fire(spec: JevSpec) -> None:
    assert decide(spec, _output(spec, area="api", confidence=0.5), False).escalate is False


def test_answer_in_on_a_choice_skips(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="ui"), False)
    assert decision == Decision(skip=True, escalate=False, route=(), reason="docs_only")


def test_answer_in_on_a_score_escalates_and_routes(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="api", size="large"), False)
    assert decision == Decision(skip=False, escalate=True, route=("code-reviewer",), reason="huge")


def test_answer_in_ignores_an_answer_not_listed(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="api", size="medium"), False)
    assert decision == Decision(skip=False, escalate=False, route=(), reason=NO_GATE)


# --- combining gates


def test_an_escalation_clears_a_skip(spec: JevSpec) -> None:
    # `calm` and `docs_only` skip, `huge` escalates.
    decision = decide(spec, _output(spec, p=0.01, area="ui", size="large"), False)
    assert decision.skip is False
    assert decision.escalate is True
    assert decision.reason == "huge"


def _with_unless(**changes: Any) -> JevSpec:
    """`spec` with `unless: calm` on the `unsure` gate, then `changes` on that gate."""
    gates = copy.deepcopy(MANIFEST["gates"])
    unsure = next(gate for gate in gates if gate["name"] == "unsure")
    unsure.update({"unless": "calm", **changes})
    return _spec(gates=gates)


def test_a_gate_with_unless_is_held_back_when_the_named_gate_fires() -> None:
    # `calm` skips and `unsure` would escalate, but `unsure` yields to `calm`.
    decision = decide(_with_unless(), _output(_with_unless(), p=0.01, confidence=0.3), False)
    assert decision == Decision(skip=True, escalate=False, route=(), reason="calm")


def test_a_gate_with_unless_fires_when_the_named_gate_does_not() -> None:
    decision = decide(_with_unless(), _output(_with_unless(), p=0.5, confidence=0.3), False)
    assert decision.escalate is True and decision.skip is False
    assert decision.reason == "unsure"
    assert decision.route == ("code-reviewer", "security-reviewer")


def test_unless_names_one_gate_so_other_skips_are_still_cleared() -> None:
    # `docs_only` skips on area=ui; `unsure` yields only to `calm`, which does not fire at p=0.5.
    decision = decide(_with_unless(), _output(_with_unless(), p=0.5, confidence=0.3), False)
    assert decision.skip is False and decision.escalate is True


def test_unless_is_read_before_manifest_order() -> None:
    # `unsure` is listed before `calm` here and still yields to it.
    gates = copy.deepcopy(MANIFEST["gates"])
    calm = next(gate for gate in gates if gate["name"] == "calm")
    unsure = next(gate for gate in gates if gate["name"] == "unsure")
    unsure["unless"] = "calm"
    reordered = [unsure, *[gate for gate in gates if gate is not unsure and gate is not calm], calm]
    spec = _spec(gates=reordered)
    decision = decide(spec, _output(spec, p=0.01, confidence=0.3, area="api"), False)
    assert decision == Decision(skip=True, escalate=False, route=(), reason="calm")


def test_the_first_escalating_gate_names_the_reason(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, p=0.95, area="api", size="large"), False)
    assert decision.reason == "alarm"


def test_the_first_skipping_gate_names_the_reason_when_nothing_escalates(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, p=0.01, area="ui"), False)
    assert decision == Decision(skip=True, escalate=False, route=(), reason="calm")


def test_no_gate_and_no_route_reviews_everything_by_default(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="api"), False)
    assert decision == Decision(skip=False, escalate=False, route=(), reason=NO_GATE)


# --- routing


def test_routing_by_answer(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="db"), False)
    assert decision == Decision(
        skip=False, escalate=False, route=("perf-reviewer",), reason="area=db"
    )


def test_routing_by_gate_name_and_by_answer_are_joined_in_manifest_order(spec: JevSpec) -> None:
    # `unsure` (gate) comes first in the manifest, then `area=db`, then `size=large`.
    decision = decide(spec, _output(spec, area="db", confidence=0.1, size="large"), False)
    assert decision.route == ("code-reviewer", "security-reviewer", "perf-reviewer")
    assert decision.reason == "unsure"


def test_the_route_has_no_duplicates(spec: JevSpec) -> None:
    # `unsure` gives code + security; `size=large` gives code again.
    decision = decide(spec, _output(spec, area="api", confidence=0.1, size="large"), False)
    assert decision.route == ("code-reviewer", "security-reviewer")


def test_an_escalating_gate_names_the_reason_over_a_matching_route(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="db", size="large"), False)
    assert decision.reason == "huge"
    assert decision.route == ("perf-reviewer", "code-reviewer")


# --- rules that always hold


def test_a_truncated_input_never_skips(spec: JevSpec) -> None:
    output = _output(spec, p=0.01, area="ui")
    assert decide(spec, output, False).skip is True
    decision = decide(spec, output, True)
    assert decision.skip is False
    assert decision.reason == "truncated"


def test_a_truncated_input_escalates_and_does_not_narrow_the_route(spec: JevSpec) -> None:
    decision = decide(spec, _output(spec, area="db"), True)
    assert decision == Decision(skip=False, escalate=True, route=(), reason="truncated")


def test_a_jev_failure_escalates() -> None:
    assert jev_error() == Decision(skip=False, escalate=True, route=(), reason="jev_error")


def test_a_manifest_without_gates_or_routing_decides_nothing() -> None:
    bare = _spec(gates=[], routing=[])
    assert decide(bare, _output(bare), False) == Decision(
        skip=False, escalate=False, route=(), reason=NO_GATE
    )


def _triage_output(spec: JevSpec, p: float, confidence: float) -> JevOutput:
    return spec.output_class.model_validate(
        {
            "needs_review": {"p": p},
            "reviewers": {
                "answer": "code",
                "probs": {"code": 0.4, "code-security": 0.2, "code-perf": 0.2, "all": 0.2},
                "confidence": confidence,
            },
        }
    )


def test_the_diff_triage_manifest_escalates_on_low_confidence() -> None:
    from registry.resolver import resolve

    triage = resolve("diff-triage", "^0.3")
    assert isinstance(triage, JevSpec)
    decision = decide(triage, _triage_output(triage, p=0.7, confidence=0.3), False)
    assert decision.escalate is True and decision.skip is False
    assert decision.reason == "unsure"
    assert decision.route == ("code-reviewer", "security-reviewer", "perf-reviewer")


def test_the_diff_triage_manifest_skips_low_risk_whatever_the_routing_confidence() -> None:
    from registry.resolver import resolve

    triage = resolve("diff-triage", "^0.3")
    assert isinstance(triage, JevSpec)
    decision = decide(triage, _triage_output(triage, p=0.02, confidence=0.3), False)
    assert decision.skip is True and decision.escalate is False
    assert decision.reason == "low_risk"
    assert decision.route == ("code-reviewer",)


# --- decide_judge_gate


def test_a_skip_is_reported_as_the_judge_gate_whichever_gate_fired(spec: JevSpec) -> None:
    output = _output(spec, p=0.05, area="api")
    assert decide(spec, output, False).reason == "calm"
    decision = decide_judge_gate(spec, output, False)
    assert decision == Decision(skip=True, escalate=False, route=(), reason=JUDGE_GATE)


def test_an_escalation_keeps_the_reason_the_gate_gave(spec: JevSpec) -> None:
    output = _output(spec, p=0.05, confidence=0.2)
    assert decide_judge_gate(spec, output, False) == decide(spec, output, False)
    assert decide_judge_gate(spec, output, False).reason == "unsure"


def test_nothing_fired_leaves_the_judge_running(spec: JevSpec) -> None:
    decision = decide_judge_gate(spec, _output(spec, p=0.5, area="api"), False)
    assert (decision.skip, decision.escalate, decision.reason) == (False, False, NO_GATE)


def test_a_truncated_input_never_skips_the_judge(spec: JevSpec) -> None:
    decision = decide_judge_gate(spec, _output(spec, p=0.05, area="api"), True)
    assert decision == Decision(skip=False, escalate=True, route=(), reason=TRUNCATED)


# --- skip_gate and with_skip_threshold

ONE_SKIP = [
    {"name": "alarm", "question": "risky", "rule": "p_above", "value": 0.9, "action": "escalate"},
    {"name": "calm", "question": "risky", "rule": "p_below", "value": 0.1, "action": "skip"},
    {
        "name": "unsure",
        "question": "area",
        "rule": "confidence_below",
        "value": 0.5,
        "action": "escalate",
    },
]
NO_SKIP = [gate for gate in ONE_SKIP if gate["action"] != "skip"]


def test_skip_gate_returns_the_one_p_below_skip_gate() -> None:
    gate = skip_gate(_spec(gates=ONE_SKIP))
    assert gate is not None
    assert (gate.name, gate.question, gate.value) == ("calm", "risky", 0.1)


def test_skip_gate_is_none_without_a_skip_gate() -> None:
    assert skip_gate(_spec(gates=NO_SKIP)) is None


def test_skip_gate_refuses_several_skip_gates(spec: JevSpec) -> None:
    with pytest.raises(ValueError, match="calm, db_unlikely, docs_only"):
        skip_gate(spec)


def test_skip_gate_refuses_a_skip_gate_of_another_rule() -> None:
    answer_in = {
        "name": "docs",
        "question": "area",
        "rule": "answer_in",
        "value": ["ui"],
        "action": "skip",
    }
    with pytest.raises(ValueError, match="p_below"):
        skip_gate(_spec(gates=[*NO_SKIP, answer_in]))


def test_with_skip_threshold_moves_the_existing_gate_and_keeps_the_rest() -> None:
    original = _spec(gates=ONE_SKIP)
    moved = with_skip_threshold(original, 0.3)
    assert [gate.name for gate in moved.gates] == ["alarm", "calm", "unsure"]
    gate = skip_gate(moved)
    assert gate is not None and (gate.question, gate.value) == ("risky", 0.3)
    assert moved.routing == original.routing
    assert original.gates[1].value == 0.1  # the original is unchanged


def test_with_skip_threshold_adds_a_named_gate_when_there_is_none() -> None:
    added = with_skip_threshold(_spec(gates=NO_SKIP), 0.2, question="risky")
    gate = skip_gate(added)
    assert gate is not None
    assert (gate.name, gate.question, gate.option, gate.value) == (
        SKIP_GATE_NAME,
        "risky",
        None,
        0.2,
    )


def test_with_skip_threshold_reads_an_option_of_a_choice_question() -> None:
    added = with_skip_threshold(_spec(gates=NO_SKIP), 0.2, question="area", option="db")
    probs = {"ui": 0.5, "api": 0.49, "db": 0.01}
    assert decide(added, _output(added, area="api", area_probs=probs), False).skip is True


def test_with_skip_threshold_needs_a_question_when_there_is_no_gate() -> None:
    with pytest.raises(ValueError, match="no skip gate"):
        with_skip_threshold(_spec(gates=NO_SKIP), 0.2)


def test_with_skip_threshold_validates_the_copy() -> None:
    with pytest.raises(ValueError, match="'nope' is not defined"):
        with_skip_threshold(_spec(gates=NO_SKIP), 0.2, question="nope")


def test_the_moved_threshold_decides_with_the_strict_rule_and_escalations_win() -> None:
    moved = with_skip_threshold(_spec(gates=ONE_SKIP), 0.3)
    assert decide(moved, _output(moved, p=0.29, area="api"), False).skip is True
    assert decide(moved, _output(moved, p=0.3, area="api"), False).skip is False
    unsure = decide(moved, _output(moved, p=0.01, area="api", confidence=0.1), False)
    assert (unsure.skip, unsure.reason) == (False, "unsure")
    assert decide(moved, _output(moved, p=0.01, area="api"), True).skip is False
