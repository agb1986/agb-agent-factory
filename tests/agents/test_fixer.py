"""Offline tests of the fixer agent: manifest, models, prompt, eval cases."""

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

AGENT_DIR = AGENTS_DIR / "fixer"
FINDING: dict[str, Any] = {
    "file": "series/rolling.py",
    "line_start": 12,
    "line_end": 13,
    "severity": "major",
    "category": "logic",
    "title": "Loop bound drops the last window",
    "body": "The loop stops one step early.",
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("fixer", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "files_changed": [
            {"path": "series/rolling.py", "action": "modified", "summary": "loop bound"}
        ],
        "commands_run": [
            {"argv": ["python", "-m", "pytest", "-q"], "exit_code": 0, "output": "5 passed"}
        ],
        "nothing_to_fix": False,
        "suite_passes": True,
        "summary": "The loop stopped one step early; it now runs to len(values).",
    }
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "fixer@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "xhigh"
    assert spec.sandbox is True
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 25
    assert spec.limits.max_tokens_total is not None


def test_the_manifest_puts_the_agent_behind_the_action_guard(spec: LLMSpec) -> None:
    assert spec.guard == "action-guard@^0.1"


def test_tools_exist_and_need_the_sandbox_flag(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) <= set(TOOLS)
    assert "rg.search" not in spec.tools.allow
    assert "shell.run" in spec.tools.allow
    assert len(tool_defs(spec.tools.allow, spec.tools.deny, sandbox=spec.sandbox)) == len(
        spec.tools.allow
    )
    with pytest.raises(ToolListError):
        tool_defs(spec.tools.allow, spec.tools.deny)


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "FixerInput"
    assert spec.output_class.__name__ == "FixerOutput"


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
    assert set(schema["required"]) == {
        "files_changed",
        "commands_run",
        "nothing_to_fix",
        "suite_passes",
        "summary",
    }


# --- models


def test_input_needs_a_repo_a_finding_and_test_files(spec: LLMSpec) -> None:
    payload = {"repo_path": "/repo", "finding": FINDING, "test_files": ["tests/test_rolling.py"]}
    parsed = spec.input_class.model_validate(payload)
    assert parsed.model_dump()["test_files"] == ["tests/test_rolling.py"]
    with pytest.raises(ValidationError):
        spec.input_class.model_validate({"repo_path": "/repo", "finding": FINDING})
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(payload | {"extra": 1})


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output())
    assert spec.output_class.model_validate_json(output.model_dump_json()) == output


def test_a_run_with_nothing_to_fix_changes_no_file(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(
        _output(files_changed=[], nothing_to_fix=True, summary="The tests already pass.")
    )
    assert output.model_dump()["files_changed"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"suite_passes": "maybe"},
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


def test_prompt_names_every_tool_and_the_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    assert "You never change a test file" in text
    assert "`nothing_to_fix`" in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_at_least_two_cases_with_unique_ids(cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(ids) >= 2
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
        assert case.expected.run.tests == "unchanged"
        assert case.expected.run.suite == "pass"


def test_the_cases_cover_a_real_fix_and_nothing_to_fix(cases: list[Case]) -> None:
    changes = {case.expected.run.production for case in cases if case.expected.run}
    assert changes == {"changed", "unchanged"}
