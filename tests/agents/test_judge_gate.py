"""Offline tests of the judge-gate agent: manifest, gates, output model and eval cases."""

from pathlib import Path

import pytest

from evals.cases import CASES_FILE, Case, load_cases
from fixtures.build import discover
from pipelines.review_panel import JUDGE_GATE
from registry.jev_spec import AnswerInGate, ChoiceQuestion, ConfidenceGate, JevSpec, ProbabilityGate
from registry.resolver import AGENTS_DIR, resolve
from runtime.gates import decide_judge_gate

AGENT_DIR = AGENTS_DIR / "judge-gate"


@pytest.fixture(scope="module")
def spec() -> JevSpec:
    found = resolve(*JUDGE_GATE)
    assert isinstance(found, JevSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / CASES_FILE)


def _answers(p: float, answer: str, confidence: float) -> dict[str, object]:
    rest = (1.0 - confidence) / 2
    probs = {option: rest for option in ("ship", "request_changes", "unclear")}
    probs[answer] = confidence
    return {
        "clear_cut": {"p": p},
        "verdict": {"answer": answer, "probs": probs, "confidence": confidence},
    }


# --- the manifest


def test_the_manifest_validates_and_the_panel_range_resolves_it(spec: JevSpec) -> None:
    assert spec.address == "judge-gate@0.1.0"
    assert spec.kind == "jev"
    assert spec.directory == AGENT_DIR
    assert JUDGE_GATE == ("judge-gate", "^0.1")


def test_the_questions_are_clear_cut_and_the_verdict(spec: JevSpec) -> None:
    assert [(q.id, q.type) for q in spec.questions] == [
        ("clear_cut", "noul"),
        ("verdict", "choice"),
    ]
    verdict_question = spec.questions[1]
    assert isinstance(verdict_question, ChoiceQuestion)
    assert verdict_question.options == ("ship", "request_changes", "unclear")


def test_the_gates_carry_the_thresholds(spec: JevSpec) -> None:
    skip, unsure, unclear = spec.gates
    assert isinstance(skip, ProbabilityGate)
    assert (skip.question, skip.rule, skip.value, skip.action) == (
        "clear_cut", "p_above", 0.85, "skip"
    )  # fmt: skip
    assert isinstance(unsure, ConfidenceGate)
    assert (unsure.question, unsure.value, unsure.action) == ("verdict", 0.8, "escalate")
    assert isinstance(unclear, AnswerInGate)
    assert (unclear.question, unclear.value, unclear.action) == (
        "verdict",
        ("unclear",),
        "escalate",
    )


def test_the_output_round_trips_through_the_output_model(spec: JevSpec) -> None:
    answers = _answers(0.95, "ship", 0.95)
    output = spec.output_class.model_validate(answers)
    assert output.model_dump(mode="json") == answers
    assert spec.output_class.model_validate(output.model_dump(mode="json")) == output


def test_the_output_model_rejects_an_option_the_manifest_does_not_list(spec: JevSpec) -> None:
    with pytest.raises(ValueError):
        spec.output_class.model_validate(_answers(0.9, "merge", 0.9))


def test_the_input_renders_the_reviews(spec: JevSpec) -> None:
    assert "{reviews}" in spec.input.render
    assert spec.input.max_chars > 0


# --- the decision the gates give


def test_a_clear_ship_skips_the_judge(spec: JevSpec) -> None:
    output = spec.output_class.model_validate(_answers(0.95, "ship", 0.95))
    decision = decide_judge_gate(spec, output, truncated=False)
    assert (decision.skip, decision.escalate, decision.reason) == (True, False, "judge_gate")


def test_a_clear_block_skips_the_judge_too(spec: JevSpec) -> None:
    output = spec.output_class.model_validate(_answers(0.9, "request_changes", 0.9))
    assert decide_judge_gate(spec, output, truncated=False).skip is True


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        (_answers(0.5, "ship", 0.95), "no_gate"),
        (_answers(0.95, "ship", 0.7), "unsure"),
        (_answers(0.95, "unclear", 0.95), "unclear"),
    ],
)
def test_anything_short_of_clear_cut_runs_the_judge(
    spec: JevSpec, answers: dict[str, object], reason: str
) -> None:
    decision = decide_judge_gate(spec, spec.output_class.model_validate(answers), truncated=False)
    assert decision.skip is False
    assert decision.reason == reason


# --- the eval cases


def test_there_are_at_least_four_cases_and_all_use_the_jev_grader(cases: list[Case]) -> None:
    assert len(cases) >= 4
    assert {case.grader for case in cases} == {"jev"}


def test_every_case_hands_the_gate_reviews_and_expects_a_verdict_or_states_criteria(
    cases: list[Case],
) -> None:
    for case in cases:
        assert set(case.payload) == {"reviews"}, case.id
        if case.expected.verdict is None:
            # the model's verdict on contradictory reviewers is not pinned, so the grader needs
            # the prose to know what the gate must and must not do
            assert case.expected.criteria, case.id
        else:
            assert case.expected.verdict in {"ship", "request_changes", "unclear"}, case.id


def test_the_cases_cover_a_skip_a_block_and_the_cases_that_must_not_skip(
    cases: list[Case],
) -> None:
    expected = {case.id: case.expected.verdict for case in cases}
    assert expected["clean-refactor-ships"] == "ship"
    assert "request_changes" in expected.values()
    assert "unclear" in expected.values()


def test_every_case_payload_renders_into_the_input(spec: JevSpec, cases: list[Case]) -> None:
    for case in cases:
        assert spec.input.render.format(**case.payload), case.id


def test_cases_reference_existing_fixtures(cases: list[Case]) -> None:
    fixtures = discover()
    for case in cases:
        assert case.fixture in fixtures, case.id


def test_the_cases_file_is_next_to_the_manifest() -> None:
    assert (AGENT_DIR / CASES_FILE).is_file()
    assert (AGENT_DIR / "agent.yaml").is_file()
    assert Path(AGENT_DIR).name == "judge-gate"
