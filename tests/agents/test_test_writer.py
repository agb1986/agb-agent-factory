"""Offline tests of the test-writer agent: manifest, models, prompt, eval cases."""

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

AGENT_DIR = AGENTS_DIR / "test-writer"
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
    found = resolve("test-writer", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "files_changed": [
            {"path": "tests/test_last.py", "action": "created", "summary": "last window"}
        ],
        "commands_run": [
            {"argv": ["python", "-m", "pytest", "-q", "tests/test_last.py"], "exit_code": 1,
             "output": "1 failed"}
        ],
        "test_files": ["tests/test_last.py"],
        "fails_on_head": True,
        "summary": "The test fails on the dropped window.",
    }  # fmt: skip
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "test-writer@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "xhigh"
    assert spec.sandbox is True
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 25
    assert spec.limits.max_tokens_total is not None


def test_the_manifest_declares_no_guard_yet(spec: LLMSpec) -> None:
    assert spec.guard is None


def test_tools_exist_and_need_the_sandbox_flag(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) <= set(TOOLS)
    assert "rg.search" not in spec.tools.allow
    assert "fs.write_file" in spec.tools.allow
    assert len(tool_defs(spec.tools.allow, spec.tools.deny, sandbox=spec.sandbox)) == len(
        spec.tools.allow
    )
    with pytest.raises(ToolListError):
        tool_defs(spec.tools.allow, spec.tools.deny)


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "TestWriterInput"
    assert spec.output_class.__name__ == "TestWriterOutput"


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
        "test_files",
        "fails_on_head",
        "summary",
    }


# --- models


def test_input_needs_a_repo_and_a_finding(spec: LLMSpec) -> None:
    parsed = spec.input_class.model_validate({"repo_path": "/repo", "finding": FINDING})
    assert parsed.model_dump()["finding"]["file"] == "series/rolling.py"
    with pytest.raises(ValidationError):
        spec.input_class.model_validate({"repo_path": "/repo"})
    with pytest.raises(ValidationError):
        spec.input_class.model_validate({"repo_path": "/repo", "finding": FINDING, "extra": 1})


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output())
    assert spec.output_class.model_validate_json(output.model_dump_json()) == output


@pytest.mark.parametrize(
    "bad",
    [
        {"fails_on_head": "maybe"},
        {"files_changed": [{"path": "a.py", "action": "renamed", "summary": "s"}]},
        {"commands_run": [{"argv": "pytest", "exit_code": 0, "output": ""}]},
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
    assert "You change test files only" in text
    assert "no network" in text.lower()


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


def test_the_cases_cover_a_failing_test_and_a_coverage_test(cases: list[Case]) -> None:
    kinds = {case.expected.run.new_tests for case in cases if case.expected.run}
    assert kinds == {"fail", "pass"}
    assert all(case.expected.run.production == "unchanged" for case in cases if case.expected.run)
