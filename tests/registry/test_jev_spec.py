"""`JevSpec`: every validation rule of `kind: jev`, and the output models generated from it."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from registry import JevOutput, JevSpec, ManifestError, load_spec, resolve
from registry.jev_spec import InputSpec

DIFF_TRIAGE = Path(__file__).parents[2] / "agents" / "diff-triage" / "agent.yaml"

VALID: dict[str, Any] = {
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
            "name": "api_likely",
            "question": "area",
            "rule": "p_above",
            "option": "api",
            "value": 0.7,
            "action": "escalate",
        },
        {
            "name": "unsure",
            "question": "area",
            "rule": "confidence_below",
            "value": 0.5,
            "action": "escalate",
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
        {"when": "area=db", "route": ["perf-reviewer"]},
        {"when": "size=large", "route": ["code-reviewer"]},
    ],
    "evals": {"min_pass_rate": 0.9, "min_recall_risky": 0.98},
}


def spec_from(**changes: Any) -> JevSpec:
    """Validate `VALID` with top-level keys replaced; a value of `...` removes the key."""
    data = copy.deepcopy(VALID)
    for key, value in changes.items():
        if value is ...:
            del data[key]
        else:
            data[key] = value
    return JevSpec.model_validate({**data, "path": Path("agent.yaml")})


def rejects(match: str, **changes: Any) -> None:
    with pytest.raises(ValidationError, match=match):
        spec_from(**changes)


def question(**fields: Any) -> list[dict[str, Any]]:
    return [{"id": "q", "text": "Why?", **fields}]


def test_valid_manifest_round_trips() -> None:
    spec = spec_from()
    assert [q.id for q in spec.questions] == ["risky", "area", "size"]
    assert [g.name for g in spec.gates] == ["calm", "api_likely", "unsure", "huge"]
    assert spec.routing[1].route == ("perf-reviewer",)
    again = JevSpec.model_validate(spec.model_dump())
    assert again == spec


def test_gates_and_routing_are_optional() -> None:
    spec = spec_from(gates=[], routing=[])
    assert spec.gates == () and spec.routing == ()
    data = {k: v for k, v in VALID.items() if k not in ("gates", "routing")}
    assert JevSpec.model_validate({**data, "path": Path("a.yaml")}).routing == ()


def test_loads_through_the_resolver(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    (agents / "triage").mkdir(parents=True)
    (agents / "triage" / "agent.yaml").write_text(yaml.safe_dump(VALID))
    spec = resolve("triage", "^1.0", agents)
    assert isinstance(spec, JevSpec)
    assert spec.input.fields == ("diff",)
    assert spec.evals.min_recall_risky == 0.98


def test_resolver_reports_a_bad_jev_manifest_with_the_gate(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["gates"][2]["question"] = "nope"
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(
        ManifestError, match=r"gates\[2\] \(unsure\): question 'nope' is not defined"
    ):
        load_spec(path)


def test_real_diff_triage_manifest_loads() -> None:
    spec = load_spec(DIFF_TRIAGE)
    assert isinstance(spec, JevSpec)
    assert spec.model == "jev-1.14.0"
    ids = {q.id: q.type for q in spec.questions}
    assert ids == {"needs_review": "noul", "reviewers": "choice"}
    assert set(spec.output_class.model_fields) == {"needs_review", "reviewers"}
    assert spec.evals.min_recall_risky == 0.98


@pytest.mark.parametrize("model", ["jev-latest", "jev-preview", "jev-1.14", "jev-v1.14.0", "x"])
def test_model_must_be_a_pinned_version(model: str) -> None:
    rejects("model", model=model)


def test_unknown_keys_are_errors() -> None:
    rejects("Extra inputs", extra_key=1)
    data = copy.deepcopy(VALID["questions"])
    data[0]["extra"] = 1
    rejects("Extra inputs", questions=data)


def test_questions_are_required_and_non_empty() -> None:
    rejects("questions", questions=...)
    rejects("questions", questions=[])


def test_question_ids_are_unique() -> None:
    qs = question(type="noul") + question(type="noul")
    rejects("duplicate id 'q'", questions=qs)


@pytest.mark.parametrize("bad", ["Risky", "1st", "has-dash", "model_x", "class", "", "a__b"])
def test_question_id_must_be_a_usable_field_name(bad: str) -> None:
    rejects("id", questions=question(type="noul", id=bad))


def test_question_type_must_be_known() -> None:
    rejects("type", questions=question(type="boolean"))


@pytest.mark.parametrize("count", [0, 1, 11])
def test_choice_needs_two_to_ten_options(count: int) -> None:
    options = [f"o{i}" for i in range(count)]
    rejects("options", questions=question(type="choice", options=options))


@pytest.mark.parametrize("count", [2, 10])
def test_choice_accepts_the_limits(count: int) -> None:
    options = [f"o{i}" for i in range(count)]
    assert spec_from(questions=question(type="choice", options=options), gates=[], routing=[])


def test_choice_options_are_unique_and_non_blank() -> None:
    rejects("duplicate options: 'a'", questions=question(type="choice", options=["a", "b", "a"]))
    rejects("options", questions=question(type="choice", options=["a", ""]))
    rejects("options", questions=question(type="choice", options=["a", " b"]))


def test_choice_needs_options_and_noul_forbids_them() -> None:
    rejects("options", questions=question(type="choice"))
    rejects("Extra inputs", questions=question(type="noul", options=["a", "b"]))


@pytest.mark.parametrize("bounds", [[1, 1], [2, 1], [0, float("inf")], [float("nan"), 1]])
def test_score_range_must_be_finite_with_min_below_max(bounds: list[float]) -> None:
    rejects("range", questions=question(type="score", range=bounds, levels=["lo", "hi"]))


def test_score_range_has_two_numbers() -> None:
    rejects("range", questions=question(type="score", range=[0], levels=["lo", "hi"]))
    rejects("range", questions=question(type="score", levels=["lo", "hi"]))


@pytest.mark.parametrize("count", [0, 1, 11])
def test_score_needs_two_to_ten_levels(count: int) -> None:
    levels = [f"l{i}" for i in range(count)]
    rejects("levels", questions=question(type="score", range=[0, 1], levels=levels))


def test_score_levels_are_unique() -> None:
    rejects(
        "duplicate levels: 'a'",
        questions=question(type="score", range=[0, 1], levels=["a", "b", "a"]),
    )


def test_gate_names_are_unique() -> None:
    gate = VALID["gates"][0]
    rejects("duplicate gate name", gates=[gate, gate])


def test_gate_unless_names_another_gate() -> None:
    gates = copy.deepcopy(VALID["gates"])
    gates[2]["unless"] = "calm"
    assert spec_from(gates=gates).gates[2].unless == "calm"
    assert spec_from().gates[2].unless is None


def test_gate_unless_must_name_a_defined_gate() -> None:
    gates = copy.deepcopy(VALID["gates"])
    gates[2]["unless"] = "ghost"
    rejects(r"gates\[2\] \(unsure\): unless names no gate 'ghost'", gates=gates)


def test_gate_unless_cannot_name_itself() -> None:
    gates = copy.deepcopy(VALID["gates"])
    gates[2]["unless"] = "unsure"
    rejects(r"gates\[2\] \(unsure\): unless names the gate itself", gates=gates)


def test_gate_unless_does_not_chain() -> None:
    gates = copy.deepcopy(VALID["gates"])
    gates[1]["unless"] = "calm"
    gates[2]["unless"] = "api_likely"
    rejects(
        r"gates\[2\] \(unsure\): unless names 'api_likely', which has an unless of its own",
        gates=gates,
    )


def test_gate_must_name_a_defined_question() -> None:
    gate = {**VALID["gates"][0], "question": "missing"}
    rejects(r"gates\[0\] \(calm\): question 'missing' is not defined", gates=[gate])


def test_gate_rule_and_action_must_be_known() -> None:
    rejects("rule", gates=[{**VALID["gates"][0], "rule": "p_equal"}])
    rejects("action", gates=[{**VALID["gates"][0], "action": "ignore"}])


@pytest.mark.parametrize("value", [-0.1, 1.1, "high", float("nan")])
def test_probability_value_is_between_zero_and_one(value: Any) -> None:
    rejects("value", gates=[{**VALID["gates"][0], "value": value}])
    rejects("value", gates=[{**VALID["gates"][2], "value": value}])


def test_confidence_below_is_invalid_on_noul() -> None:
    gate = {**VALID["gates"][2], "question": "risky"}
    rejects("confidence_below is invalid on noul question 'risky'", gates=[gate], routing=[])


def test_confidence_below_is_valid_on_choice_and_score() -> None:
    gate = {**VALID["gates"][2], "question": "size"}
    assert spec_from(gates=[gate], routing=[])


def test_answer_in_is_invalid_on_noul() -> None:
    gate = {**VALID["gates"][3], "question": "risky", "value": ["yes"]}
    rejects("answer_in is invalid on noul question 'risky'", gates=[gate], routing=[])


def test_answer_in_values_must_be_options_or_levels() -> None:
    area = {**VALID["gates"][3], "question": "area", "value": ["ui", "mobile"]}
    rejects("'area' has no answer 'mobile'", gates=[area], routing=[])
    size = {**VALID["gates"][3], "value": ["huge"]}
    rejects("'size' has no answer 'huge'", gates=[size], routing=[])


def test_answer_in_needs_unique_values() -> None:
    rejects("value", gates=[{**VALID["gates"][3], "value": []}], routing=[])
    rejects("duplicate answers", gates=[{**VALID["gates"][3], "value": ["large", "large"]}])


def test_probability_gate_on_a_choice_or_score_needs_a_real_option() -> None:
    gate = VALID["gates"][1]
    rejects("p_above on 'area' needs an option", gates=[{**gate, "option": None}], routing=[])
    rejects("'area' has no answer 'mobile'", gates=[{**gate, "option": "mobile"}], routing=[])
    on_score = {**gate, "question": "size", "option": "large"}
    assert spec_from(gates=[on_score], routing=[])
    rejects("'size' has no answer 'api'", gates=[{**on_score, "option": "api"}], routing=[])


def test_probability_gate_on_a_noul_takes_no_option() -> None:
    rejects("option is invalid on noul", gates=[{**VALID["gates"][0], "option": "yes"}])


def test_option_is_not_a_key_of_the_other_rules() -> None:
    rejects("Extra inputs", gates=[{**VALID["gates"][2], "option": "api"}])
    rejects("Extra inputs", gates=[{**VALID["gates"][3], "option": "large"}])


def test_routing_when_a_gate_name() -> None:
    assert spec_from(routing=[{"when": "huge", "route": ["code-reviewer"]}])
    rejects("no gate named 'ghost'", routing=[{"when": "ghost", "route": ["code-reviewer"]}])


def test_routing_when_question_equals_answer() -> None:
    entry = {"route": ["code-reviewer"]}
    assert spec_from(routing=[{"when": "area=api", **entry}, {"when": "size=small", **entry}])
    rejects("'area' has no answer 'mobile'", routing=[{"when": "area=mobile", **entry}])
    rejects("question 'ghost' is not defined", routing=[{"when": "ghost=a", **entry}])
    rejects("noul question 'risky' has no answers", routing=[{"when": "risky=yes", **entry}])


def test_routing_entries_are_unique_per_when() -> None:
    entry = {"when": "huge", "route": ["code-reviewer"]}
    rejects("duplicate entry", routing=[entry, entry])


def test_route_is_a_non_empty_list_of_unique_agent_names() -> None:
    rejects("route", routing=[{"when": "huge", "route": []}])
    rejects("route", routing=[{"when": "huge", "route": ["Code_Reviewer"]}])
    rejects("duplicate agents", routing=[{"when": "huge", "route": ["a", "a"]}])


def test_problems_are_reported_together() -> None:
    gates = [{**VALID["gates"][0], "question": "x"}, {**VALID["gates"][1], "option": "mobile"}]
    with pytest.raises(ValidationError) as caught:
        spec_from(gates=gates, routing=[{"when": "ghost", "route": ["a"]}])
    message = str(caught.value)
    assert "gates[0]" in message and "gates[1]" in message and "routing[0]" in message


@pytest.mark.parametrize("render", ["", "no fields", "{}", "{0}", "{a.b}", "{a[0]}", "{a", "a}"])
def test_render_template_must_use_plain_payload_fields(render: str) -> None:
    with pytest.raises(ValidationError, match="render"):
        InputSpec(render=render, max_chars=10)


def test_render_template_rejects_nested_fields_in_a_spec() -> None:
    with pytest.raises(ValidationError, match="nests a field"):
        InputSpec(render="{a:{b}}", max_chars=10)


def test_render_template_fields_in_order_without_repeats() -> None:
    spec = InputSpec(render="{files}\n{diff}\n{files!r} {{literal}}", max_chars=10)
    assert spec.fields == ("files", "diff")


@pytest.mark.parametrize("limit", [0, -1, 1.5, "ten"])
def test_max_chars_is_a_positive_integer(limit: Any) -> None:
    with pytest.raises(ValidationError, match="max_chars"):
        InputSpec(render="{a}", max_chars=limit)


@pytest.mark.parametrize("rate", [-0.1, 1.1])
def test_evals_rates_are_probabilities(rate: float) -> None:
    rejects("min_pass_rate", evals={"min_pass_rate": rate})
    rejects("min_recall_risky", evals={"min_pass_rate": 0.9, "min_recall_risky": rate})


def test_output_model_has_one_field_per_question() -> None:
    output = spec_from().output_class
    assert issubclass(output, JevOutput)
    assert output.__name__ == "TriageOutput"
    assert list(output.model_fields) == ["risky", "area", "size"]


def test_output_class_is_built_once() -> None:
    spec = spec_from()
    assert spec.output_class is spec.output_class


def test_output_model_validates_a_full_answer() -> None:
    output = spec_from().output_class.model_validate(
        {
            "risky": {"p": 0.8},
            "area": {
                "answer": "api",
                "probs": {"ui": 0.1, "api": 0.8, "db": 0.1},
                "confidence": 0.7,
            },
            "size": {
                "value": 1.4,
                "level": "medium",
                "probs": {"small": 0.2, "medium": 0.5, "large": 0.3},
                "confidence": 0.6,
            },
        }
    )
    dumped = output.model_dump()
    assert dumped["risky"]["p"] == 0.8
    assert dumped["area"]["answer"] == "api"
    assert dumped["size"]["level"] == "medium"


def answer(**changes: Any) -> dict[str, Any]:
    full: dict[str, Any] = {
        "risky": {"p": 0.5},
        "area": {"answer": "ui", "probs": {"ui": 0.6, "api": 0.2, "db": 0.2}, "confidence": 0.5},
        "size": {
            "value": 0.0,
            "level": "small",
            "probs": {"small": 0.8, "medium": 0.1, "large": 0.1},
            "confidence": 0.9,
        },
    }
    full.update(changes)
    return full


def test_output_model_rejects_what_the_question_cannot_answer() -> None:
    output = spec_from().output_class
    assert output.model_validate(answer())
    bad_answers = [
        answer(risky={"p": 1.5}),
        answer(risky={"p": 0.5, "confidence": 0.5}),
        answer(area={"answer": "mobile", "probs": {"ui": 1, "api": 0, "db": 0}, "confidence": 1}),
        answer(area={"answer": "ui", "probs": {"ui": 1, "api": 0}, "confidence": 1}),
        answer(
            area={"answer": "ui", "probs": {"ui": 1, "api": 0, "db": 0, "x": 0}, "confidence": 1}
        ),
        answer(area={"answer": "ui", "probs": {"ui": 1, "api": 0, "db": 2}, "confidence": 1}),
        answer(area={"answer": "ui", "probs": {"ui": 1, "api": 0, "db": 0}}),
        answer(
            size={
                "value": 0,
                "level": "huge",
                "probs": {"small": 1, "medium": 0, "large": 0},
                "confidence": 1,
            }
        ),
    ]
    for bad in bad_answers:
        with pytest.raises(ValidationError):
            output.model_validate(bad)
    with pytest.raises(ValidationError):
        output.model_validate({k: v for k, v in answer().items() if k != "size"})
    with pytest.raises(ValidationError):
        output.model_validate({**answer(), "extra": {"p": 0.1}})


def test_output_models_are_immutable() -> None:
    output = spec_from().output_class.model_validate(answer())
    field = "risky"
    with pytest.raises(ValidationError):
        setattr(output, field, None)
