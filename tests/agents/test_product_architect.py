"""Offline tests of the product-architect agent: manifest, models, prompt, eval cases."""

from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from fixtures.build import discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, ToolListError, tool_defs

AGENT_DIR = AGENTS_DIR / "product-architect"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("product-architect", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "problem": "Customers can only take a fixed amount off an invoice.",
        "goals": ["An invoice can carry a percentage discount."],
        "non_goals": ["Discount codes."],
        "user_stories": [
            {"as_a": "a customer", "i_want": "10% off", "so_that": "I pay less"}
        ],
        "constraints": ["billing/invoice.py applies tax before the discount."],
        "risks": ["Existing totals change if the order of tax and discount moves."],
        "open_questions": [
            {"question": "Before or after tax?", "why_it_matters": "It changes the total."}
        ],
        "related_files": ["billing/invoice.py"],
    }  # fmt: skip
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "product-architect@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "high"
    assert spec.sandbox is False
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 10
    assert spec.limits.max_tokens_total is not None


def test_tools_are_the_four_read_only_ones(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) == {
        "git.read_file",
        "rg.search",
        "deps.graph",
        "memory.search",
    }
    assert set(spec.tools.allow) <= set(TOOLS)
    assert len(tool_defs(spec.tools.allow, spec.tools.deny)) == len(spec.tools.allow)


def test_memory_search_keeps_the_agent_out_of_the_sandbox(spec: LLMSpec) -> None:
    with pytest.raises(ToolListError):
        tool_defs(spec.tools.allow, spec.tools.deny, sandbox=True)


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "PRDInput"
    assert spec.output_class.__name__ == "PRDOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert request["model"] == "claude-sonnet-5-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {
        "problem",
        "goals",
        "non_goals",
        "user_stories",
        "constraints",
        "risks",
        "open_questions",
        "related_files",
    }


# --- models


def test_input_needs_a_repo_a_ref_and_an_idea(spec: LLMSpec) -> None:
    parsed = spec.input_class.model_validate({"repo_path": "/repo", "ref": "head", "idea": "x"})
    assert parsed.model_dump()["feedback"] == []
    for bad in (
        {"repo_path": "/repo", "ref": "head"},
        {"repo_path": "/repo", "ref": "head", "idea": ""},
        {"repo_path": "/repo", "ref": "", "idea": "x"},
        {"repo_path": "/repo", "ref": "head", "idea": "x", "extra": 1},
    ):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(bad)


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output())
    assert spec.output_class.model_validate_json(output.model_dump_json()) == output


def test_a_contradictory_request_may_have_no_goals(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output(goals=[], user_stories=[]))
    assert output.model_dump()["goals"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"problem": ""},
        {"goals": "one goal"},
        {"user_stories": [{"as_a": "a customer", "i_want": "10% off"}]},
        {"open_questions": ["Before or after tax?"]},
        {"surprise": 1},
    ],
)
def test_a_malformed_output_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 500


def test_prompt_names_every_tool_and_the_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    assert "Never invent a requirement" in text
    assert "leave `goals` empty" in text
    for field in spec.output_class.model_fields:
        assert f"`{field}`" in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_four_cases_with_unique_ids(cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(ids) >= 4
    assert len(set(ids)) == len(ids)


def test_cases_reference_existing_fixtures_and_complete_into_inputs(
    spec: LLMSpec, cases: list[Case]
) -> None:
    fixtures = discover()
    for case in cases:
        assert case.fixture in fixtures
        spec.input_class.model_validate({"repo_path": "/repo", **case.payload})
        assert case.grader == "rubric"
        assert case.expected.verdict is None
        assert case.expected.rubric is not None


def test_every_case_checks_that_nothing_is_invented_and_paths_exist(cases: list[Case]) -> None:
    for case in cases:
        rubric = case.expected.rubric
        assert rubric is not None
        assert "related_files" in rubric.paths_exist
        assert any(criterion.required for criterion in rubric.criteria)


def test_the_contradictory_case_wants_open_questions_and_no_goals(cases: list[Case]) -> None:
    with_no_goals = [
        case
        for case in cases
        if case.expected.rubric and "goals" in case.expected.rubric.empty_fields
    ]
    assert [case.id for case in with_no_goals] == ["contradictory-free-shipping"]
    rubric = with_no_goals[0].expected.rubric
    assert rubric is not None
    assert "open_questions" in rubric.required_fields


def test_the_cases_cover_ideas_and_raw_feedback(cases: list[Case]) -> None:
    with_feedback = [case for case in cases if case.payload.get("feedback")]
    assert len(with_feedback) >= 1
    assert len({case.fixture for case in cases}) >= 2
