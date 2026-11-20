"""Offline tests of the feature-coder agent: manifest, models, prompt, eval cases."""

from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from evals.graders.rubric import check_output
from fixtures.build import discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, ToolListError, tool_defs

AGENT_DIR = AGENTS_DIR / "feature-coder"
TICKET: dict[str, Any] = {
    "kind": "feature",
    "title": "Reserve refuses orders over a per-order limit",
    "body": "Refuse any single reservation above a limit the caller passes.",
    "acceptance": ["reserve raises ValueError when quantity is above the limit."],
    "repo": "acme/inventory",
    "ref": "head",
}
NODE = "tests/test_stock.py::test_reserve_rejects_more_than_the_limit"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("feature-coder", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "files_changed": [
            {"path": "inventory/stock.py", "action": "modified", "summary": "limit argument"},
            {"path": "tests/test_stock.py", "action": "modified", "summary": "limit tests"},
        ],
        "test_files": ["tests/test_stock.py"],
        "acceptance": [
            {"criterion": TICKET["acceptance"][0], "tests": [NODE], "untestable_reason": None}
        ],
        "commands_run": [
            {"argv": ["python", "-m", "pytest", "-q"], "exit_code": 0, "output": "4 passed"}
        ],
        "summary": "reserve in inventory/stock.py takes a limit; test_stock.py checks it.",
        "open_questions": [],
        "blocked": False,
        "blocked_reason": None,
    }
    return output | overrides


def _blocked(**overrides: Any) -> dict[str, Any]:
    return (
        _output(
            files_changed=[],
            test_files=[],
            acceptance=[],
            summary="Read billing/invoice.py and its tests.",
            blocked=True,
            blocked_reason="The ticket says the flat discount comes off before tax; the code takes "
            "it off after tax.",
        )
        | overrides
    )


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "feature-coder@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "xhigh"
    assert spec.team == "dev"
    assert spec.sandbox is True
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tokens_total is not None


def test_the_manifest_puts_the_agent_behind_the_action_guard(spec: LLMSpec) -> None:
    assert spec.guard == "action-guard@^0.1"


def test_tools_are_the_fixers_and_need_the_sandbox_flag(spec: LLMSpec) -> None:
    fixer = resolve("fixer", "^0.1")
    assert isinstance(fixer, LLMSpec)
    assert spec.tools.allow == fixer.tools.allow
    assert set(spec.tools.allow) <= set(TOOLS)
    assert len(tool_defs(spec.tools.allow, spec.tools.deny, sandbox=spec.sandbox)) == len(
        spec.tools.allow
    )
    with pytest.raises(ToolListError):
        tool_defs(spec.tools.allow, spec.tools.deny)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_stock.py",
        "tests/conftest.py",
        "conftest.py",
        "inventory/stock.py",
        "inventory/pyproject.toml",
        "docs/usage.md",
    ],
)
def test_the_files_policy_allows_tests_and_code(spec: LLMSpec, path: str) -> None:
    assert spec.files is not None
    assert spec.files.denial(path) is None, path


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "pytest/__init__.py",
        "_pytest.py",
        "pluggy.cpython-313.so",
    ],
)
def test_the_files_policy_refuses_vcs_ci_and_the_test_run(spec: LLMSpec, path: str) -> None:
    assert spec.files is not None
    assert spec.files.denial(path) is not None, path


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "FeatureInput"
    assert spec.output_class.__name__ == "FeatureOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "xhigh"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) >= {
        "files_changed",
        "test_files",
        "acceptance",
        "summary",
        "blocked",
    }


# --- models


def test_input_carries_a_ticket_and_no_store_fields(spec: LLMSpec) -> None:
    payload = {"repo_path": "/repo", "ticket": TICKET}
    parsed = spec.input_class.model_validate(payload).model_dump()
    assert parsed["depends_on"] == []
    assert parsed["ticket"]["impacted_paths"] == []
    for field in ("status", "cost_usd", "id", "work_item_id"):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(payload | {field: "x"})
    with pytest.raises(ValidationError):
        spec.input_class.model_validate({"repo_path": "/repo"})


def test_input_rejects_a_ticket_without_acceptance_criteria(spec: LLMSpec) -> None:
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(
            {"repo_path": "/repo", "ticket": TICKET | {"acceptance": []}}
        )


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    for answer in (_output(), _blocked()):
        output = spec.output_class.model_validate(answer)
        assert spec.output_class.model_validate_json(output.model_dump_json()) == output


@pytest.mark.parametrize(
    "bad",
    [
        {"blocked": True},
        {"blocked": True, "blocked_reason": "  "},
        {"blocked_reason": "a reason on an answer that is not blocked"},
    ],
)
def test_blocked_and_its_reason_go_together(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="blocked"):
        spec.output_class.model_validate(_output(**bad))


@pytest.mark.parametrize(
    "check",
    [
        {"criterion": "c", "tests": [], "untestable_reason": None},
        {"criterion": "c", "tests": [NODE], "untestable_reason": "it is about wording"},
    ],
)
def test_a_criterion_has_tests_or_a_reason_but_not_both(
    spec: LLMSpec, check: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(acceptance=[check]))


def test_an_untestable_criterion_carries_its_reason(spec: LLMSpec) -> None:
    check = {"criterion": "c", "tests": [], "untestable_reason": "it is about a reader's judgement"}
    spec.output_class.model_validate(_output(acceptance=[check]))


@pytest.mark.parametrize(
    "bad",
    [
        {"blocked": "maybe"},
        {"files_changed": [{"path": "a.py", "action": "renamed", "summary": "s"}]},
        {"commands_run": [{"argv": ["pytest"], "exit_code": "zero", "output": ""}]},
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


def test_prompt_names_every_tool_field_and_the_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    for field in spec.output_class.model_fields:
        assert f"`{field}`" in text, field
    assert "every acceptance criterion that a test can check gets at least one test" in text
    assert "you do not guess" in text
    assert "change no file, and answer with `blocked`" in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_four_cases_with_unique_ids(cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(ids) == 4
    assert len(set(ids)) == len(ids)


def test_cases_reference_existing_fixtures_and_complete_into_inputs(
    spec: LLMSpec, cases: list[Case]
) -> None:
    fixtures = discover()
    for case in cases:
        assert case.fixture in fixtures
        spec.input_class.model_validate({"repo_path": "/repo", **case.payload})
        assert case.grader == "sandbox_tests"
        assert case.expected.run is not None
        assert case.expected.run.suite == "pass"
        assert case.expected.rubric is not None, case.id


def test_built_cases_need_new_passing_tests_and_a_mapped_answer(cases: list[Case]) -> None:
    built = [
        case for case in cases if case.expected.run and case.expected.run.production == "changed"
    ]
    assert len(built) == 3
    for case in built:
        assert case.expected.run is not None and case.expected.rubric is not None
        assert case.expected.run.new_tests == "pass"
        assert {"summary", "test_files", "acceptance"} <= set(case.expected.rubric.required_fields)
        assert "blocked_reason" in case.expected.rubric.empty_fields
        assert "test_files" in case.expected.rubric.paths_exist
        assert any(c.required for c in case.expected.rubric.criteria)


def test_one_case_is_a_contradiction_the_agent_must_refuse(cases: list[Case]) -> None:
    (case,) = [c for c in cases if c.expected.run and c.expected.run.production == "unchanged"]
    assert case.expected.run is not None and case.expected.rubric is not None
    assert case.expected.run.tests == "unchanged"
    assert "blocked_reason" in case.expected.rubric.required_fields
    assert {"files_changed", "test_files"} <= set(case.expected.rubric.empty_fields)


def test_the_rubrics_tell_a_built_answer_from_a_blocked_one(
    cases: list[Case], tmp_path: Path
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_stock.py").write_text("")
    built = next(c for c in cases if c.id == "reserve-per-order-limit").expected.rubric
    refused = next(
        c for c in cases if c.id == "discount-order-contradicts-the-code"
    ).expected.rubric
    assert built is not None and refused is not None
    assert check_output(built, _output(), tmp_path) == ()
    assert check_output(refused, _blocked(), tmp_path) == ()
    assert check_output(built, _blocked(), tmp_path)
    assert check_output(refused, _output(), tmp_path)
    assert check_output(built, _output(test_files=["tests/test_missing.py"]), tmp_path)
