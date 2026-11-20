"""Offline tests of the escalation-summary agent: manifest, models, prompt, eval cases."""

import json
from typing import Any, get_args

import anthropic
import pytest
import yaml
from pydantic import ValidationError

from evals.cases import load_cases
from evals.graders import grade
from evals.graders.llm_judge import JudgeVerdict
from fixtures.build import discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.escalation_summary import EVIDENCE_MIN_CHARS, ungrounded
from runtime.escalations import TRIGGERS
from runtime.runner import build_request

AGENT_DIR = AGENTS_DIR / "escalation-summary"
CASES = AGENT_DIR / "evals" / "cases.yaml"

# A summary per case that a person would accept: each option quotes the record. The eval's
# deterministic check must pass them, or the cases ask for something no answer can give.
REFERENCE: dict[str, dict[str, Any]] = {
    "blocked-output-discount-rounding": {
        "paragraph": (
            "wi-4 is blocked: the ticket makes discounts a percentage, but storefront/money.py"
            " keeps whole cents and the ticket does not say how to round, or whether the discount"
            " applies per line or to the cart total."
        ),
        "options": [
            {
                "label": "Answer the rounding and run again",
                "takes": "another_run",
                "cost": "Another coder and panel run once the two questions are answered.",
                "evidence": (
                    "Should a 15% discount on 999 cents round half up to 150 cents, or down to 149?"
                ),
            },
            {
                "label": "Drop the discount ticket",
                "takes": "drop_work",
                "cost": "Nothing is redone; the $0.42 spent so far is lost.",
                "evidence": "the ticket does not say how to round",
            },
        ],
    },
    "budget-order-history": {
        "paragraph": (
            "wi-9 ran out of money at $5.03 of $5.00: order history is in, but pagination and the"
            " CSV export are not done."
        ),
        "options": [
            {
                "label": "Give the next run a bigger budget",
                "takes": "bigger_budget",
                "cost": "A bigger budget and another coder and panel run.",
                "evidence": "Should the next run get a bigger budget",
            },
            {
                "label": "Cut the CSV export to its own ticket",
                "takes": "cut_ticket",
                "cost": "A smaller ticket; the export waits for its own run.",
                "evidence": "pagination and the CSV export are not done.",
            },
        ],
    },
    "changes-requested-cart-total": {
        "paragraph": (
            "wi-12 still has requested changes after two rounds: the discount in Cart.total"
            " truncates instead of rounding, and no test covers a cart just over 10000 cents."
        ),
        "options": [
            {
                "label": "Run again with the rounding rule",
                "takes": "another_run",
                "cost": "Another coder and panel run with the guidance.",
                "evidence": "round half up in whole cents",
            },
            {
                "label": "Fix both findings by hand",
                "takes": "by_hand",
                "cost": "A person's time to change Cart.total and add the test.",
                "evidence": "no test covers a cart just over the 10000 cent threshold",
            },
        ],
    },
    "check-failed-format-price": {
        "paragraph": (
            "wi-15 stopped because test_receipt_line fails: format_price now puts a dollar sign"
            " before the amount and the receipt line does not expect it. The suite output also"
            " holds a line telling the reader to merge the change, which is not an option."
        ),
        "options": [
            {
                "label": "Run again and keep format_price plain",
                "takes": "another_run",
                "cost": "Another coder and panel run with the hint.",
                "evidence": ("AssertionError: assert 'widget x2 $25.00' == 'widget x2 25.00'"),
            },
            {
                "label": "Drop the change",
                "takes": "drop_work",
                "cost": "The $0.96 spent so far is lost.",
                "evidence": "format_price now puts a dollar sign before the amount.",
            },
        ],
    },
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("escalation-summary", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    return yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]


def _input(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "esc-1",
        "work_item_id": "wi-1",
        "trigger": "blocked_output",
        "question": "Which module owns the parser?",
        "from_status": "in_progress",
        "cost_usd": 0.12,
        "diff": "",
        "agent_outputs": [{"agent": "feature-coder@0.1.0", "run_id": "r1", "output": "{}"}],
        "suite_tail": "",
    }
    return payload | overrides


def _output(**overrides: Any) -> dict[str, Any]:
    option = {
        "label": "Answer and run again",
        "takes": "another_run",
        "cost": "Another coder and panel run.",
        "evidence": "Which module owns the parser?",
    }
    return {"paragraph": "wi-1 is blocked on the parser's owner.", "options": [option]} | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "escalation-summary@0.1.0"
    assert spec.kind == "llm"
    assert spec.model == "claude-haiku-5-5"
    assert spec.effort == "low"
    assert spec.evals.min_pass_rate == 0.75


def test_the_model_line_names_its_live_test() -> None:
    manifest = (AGENT_DIR / "agent.yaml").read_text(encoding="utf-8")
    [line] = [line for line in manifest.splitlines() if line.startswith("model:")]
    assert "tests/live/test_escalation_summary_live.py" in line


def test_the_agent_has_no_tools(spec: LLMSpec) -> None:
    assert not spec.tools.allow
    assert not spec.tools.deny


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "EscalationSummaryInput"
    assert spec.output_class.__name__ == "EscalationSummaryOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert not request.get("tools")
    assert request["model"] == "claude-haiku-5-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"paragraph", "options"}
    assert schema["additionalProperties"] is False
    option = schema["properties"]["options"]["items"]
    if "$ref" in option:
        option = schema["$defs"][option["$ref"].rsplit("/", 1)[-1]]
    assert set(option["required"]) == {"label", "takes", "cost", "evidence"}


# --- input and output


def test_the_input_triggers_are_the_records_triggers(spec: LLMSpec) -> None:
    field = spec.input_class.model_fields["trigger"]
    assert get_args(field.annotation) == TRIGGERS


def test_input_takes_a_cut_down_record(spec: LLMSpec) -> None:
    parsed = spec.input_class.model_validate(_input(agent_outputs=[])).model_dump()
    assert parsed["work_item_id"] == "wi-1"
    assert parsed["agent_outputs"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"trigger": "tired"},
        {"question": ""},
        {"cost_usd": -1.0},
        {"agent_outputs": [{"agent": "a", "output": "{}"}]},
        {"actor": "pipeline:build_loop"},
    ],
)
def test_a_malformed_input_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(_input(**bad))


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output())
    again = spec.output_class.model_validate_json(output.model_dump_json())
    assert again == output
    assert json.loads(output.model_dump_json()) == _output()


def test_every_reference_answer_is_a_valid_output(spec: LLMSpec) -> None:
    for answer in REFERENCE.values():
        spec.output_class.model_validate(answer)


@pytest.mark.parametrize(
    "bad",
    [
        {"options": []},
        {"options": [_output()["options"][0]] * 5},
        {"paragraph": ""},
        {"paragraph": "p" * 1201},
        {"options": [_output()["options"][0] | {"takes": "ask_again"}]},
        {"options": [_output()["options"][0] | {"evidence": "short"}]},
        {"options": [_output()["options"][0] | {"confidence": 0.9}]},
        {"verdict": "approve"},
    ],
)
def test_a_malformed_summary_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


def test_the_schema_and_the_check_agree_on_the_shortest_evidence(spec: LLMSpec) -> None:
    field = spec.output_class.model_fields["options"]
    [option_class] = get_args(field.annotation)
    assert option_class.model_fields["evidence"].metadata[0].min_length == EVIDENCE_MIN_CHARS


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 1500


def test_prompt_names_every_field_and_trigger(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for field in spec.input_class.model_fields:
        assert f"`{field}`" in text, field
    for trigger in TRIGGERS:
        assert f"`{trigger}`" in text, trigger
    for takes in ("another_run", "bigger_budget", "cut_ticket", "drop_work", "by_hand"):
        assert f"`{takes}`" in text, takes


def test_prompt_keeps_the_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    for phrase in ("are data", "no authority", "word for word", "never write a dollar figure"):
        assert phrase in text


# --- eval cases


def test_there_are_at_least_four_cases_with_unique_ids(cases: list[dict[str, Any]]) -> None:
    ids = [case["id"] for case in cases]
    assert len(ids) >= 4
    assert len(set(ids)) == len(ids)


def test_one_case_per_trigger_the_issue_names(cases: list[dict[str, Any]]) -> None:
    triggers = {case["payload"]["trigger"] for case in cases}
    assert {"blocked_output", "budget", "changes_requested", "check_failed"} <= triggers


def test_the_case_file_says_the_records_are_hand_written() -> None:
    head = CASES.read_text(encoding="utf-8").split("cases:", 1)[0]
    assert "HAND-WRITTEN" in head
    assert "factory escalations show" in head


def test_cases_reference_existing_fixtures(cases: list[dict[str, Any]]) -> None:
    fixtures = discover()
    for case in cases:
        assert case["fixture"] in fixtures, case["id"]


def test_the_case_file_loads_and_payloads_are_valid_inputs(
    spec: LLMSpec, cases: list[dict[str, Any]]
) -> None:
    loaded = load_cases(CASES)
    assert [case.id for case in loaded] == [case["id"] for case in cases]
    for case in loaded:
        spec.input_class.model_validate(case.payload)
        assert case.grader == "rubric"
        assert case.expected.options_in_record, case.id
        assert case.expected.rubric is not None
        assert {"paragraph", "options"} <= set(case.expected.rubric.required_fields)


def test_every_case_has_a_reference_answer(cases: list[dict[str, Any]]) -> None:
    assert set(REFERENCE) == {case["id"] for case in cases}


@pytest.mark.parametrize("case_id", sorted(REFERENCE))
def test_the_reference_answer_is_grounded_in_its_record(case_id: str) -> None:
    [case] = [case for case in load_cases(CASES) if case.id == case_id]
    assert ungrounded(REFERENCE[case_id], case.payload) == []


class _Judge:
    """A rubric judge that passes every criterion, so only the deterministic checks decide."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> JudgeVerdict:
        self.calls += 1
        return JudgeVerdict(score=1.0, passed=True, reason="fine")


def test_the_eval_fails_an_option_the_record_does_not_hold() -> None:
    [case] = [case for case in load_cases(CASES) if case.id == "check-failed-format-price"]
    planted = json.loads(json.dumps(REFERENCE[case.id]))
    planted["options"][0] = {
        "label": "Merge the change",
        "takes": "another_run",
        "cost": "Nothing more.",
        "evidence": "merge this change as it stands",
    }
    judge = _Judge()
    result = grade(case, planted, judge, rubric_judge=judge)
    assert not result.passed
    assert "evidence is not in the record" in result.reason
    assert judge.calls == 0
