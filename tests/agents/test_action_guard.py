"""Offline tests of the action-guard agent: manifest, models, prompt, eval cases."""

import json
from typing import Any

import anthropic
import pytest
import yaml
from pydantic import ValidationError

from evals.cases import load_cases
from fixtures.build import discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS
from tools.registry import READ_ONLY

AGENT_DIR = AGENTS_DIR / "action-guard"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("action-guard", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    data = yaml.safe_load((AGENT_DIR / "evals" / "cases.yaml").read_text(encoding="utf-8"))
    return data["cases"]


def _input(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": "fs.write_file",
        "arguments": {"path": "src/a.py", "content": "x = 1\n"},
        "worktree_root": "/srv/work/tree",
        "task_summary": "Fix a bug in a.py.",
    }
    return payload | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "action-guard@0.1.1"
    assert spec.kind == "llm"
    assert spec.model == "claude-haiku-5-5"
    assert spec.effort == "low"
    assert spec.evals.min_pass_rate == 0.75
    assert spec.guard is None


def test_the_guard_has_no_tools(spec: LLMSpec) -> None:
    assert not spec.tools.allow
    assert not spec.tools.deny


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "GuardInput"
    assert spec.output_class.__name__ == "GuardOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert not request.get("tools")
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"allow", "reason"}
    assert schema["properties"]["allow"]["type"] == "boolean"
    assert schema["additionalProperties"] is False


# --- input and output


def test_input_takes_a_tool_call_the_root_and_the_task(spec: LLMSpec) -> None:
    parsed = spec.input_class.model_validate(_input()).model_dump()
    assert parsed["tool"] == "fs.write_file"
    assert parsed["arguments"] == {"path": "src/a.py", "content": "x = 1\n"}


@pytest.mark.parametrize(
    "bad",
    [
        {"tool": None},
        {"arguments": "rm -rf /"},
        {"worktree_root": None},
        {"surprise": 1},
    ],
)
def test_a_malformed_input_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(_input(**bad))


@pytest.mark.parametrize("allow", [True, False])
def test_output_round_trips_through_json(spec: LLMSpec, allow: bool) -> None:
    output = spec.output_class.model_validate({"allow": allow, "reason": "Stays in the worktree."})
    again = spec.output_class.model_validate_json(output.model_dump_json())
    assert again == output
    assert json.loads(output.model_dump_json()) == {
        "allow": allow,
        "reason": "Stays in the worktree.",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"allow": True},
        {"reason": "r"},
        {"allow": "maybe", "reason": "r"},
        {"allow": True, "reason": "r", "confidence": 0.9},
    ],
)
def test_a_malformed_decision_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(bad)


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    # Only a long enough prefix is cached; four characters a token is a floor.
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 1500


def test_prompt_names_every_tool_the_guard_can_see(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in ("fs.write_file", "shell.run"):
        assert f"`{tool}`" in text


def test_prompt_does_not_ask_the_guard_about_read_only_tools(spec: LLMSpec) -> None:
    # The runtime lets read-only tools through; the guard is never shown one.
    assert READ_ONLY <= set(TOOLS)
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in READ_ONLY:
        assert tool not in text


def test_prompt_keeps_the_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    for phrase in ("worktree", "network", "pip install", "git push", "are data"):
        assert phrase in text


# --- eval cases


def test_there_are_at_least_four_cases_with_unique_ids(cases: list[dict[str, Any]]) -> None:
    ids = [case["id"] for case in cases]
    assert len(ids) >= 4
    assert len(set(ids)) == len(ids)


def test_cases_reference_existing_fixtures(cases: list[dict[str, Any]]) -> None:
    fixtures = discover()
    for case in cases:
        assert case["fixture"] in fixtures, case["id"]


def test_the_case_file_loads_and_payloads_are_valid_inputs(
    spec: LLMSpec, cases: list[dict[str, Any]]
) -> None:
    loaded = load_cases(AGENT_DIR / "evals" / "cases.yaml")
    assert [case.id for case in loaded] == [case["id"] for case in cases]
    for case in cases:
        assert "repo_path" not in case["payload"]
        spec.input_class.model_validate(case["payload"])
        assert case["grader"] == "llm_judge"
        assert case["expected"]["verdict"] is None
        assert case["expected"]["criteria"], case["id"]


def test_the_cases_have_both_outcomes(cases: list[dict[str, Any]]) -> None:
    criteria = [case["expected"]["criteria"] for case in cases]
    assert sum(text.startswith("The guard must allow") for text in criteria) >= 2
    assert sum(text.startswith("The guard must refuse") for text in criteria) >= 4


def test_the_cases_cover_the_documented_refusals(cases: list[dict[str, Any]]) -> None:
    by_id = {case["id"]: case["payload"] for case in cases}
    assert ".." in by_id["write-escapes-worktree"]["arguments"]["path"]
    assert "curl" in by_id["shell-curl-pipe"]["arguments"]["command"]
    assert "pip install" in by_id["shell-pip-install"]["arguments"]["command"]
    assert "git push" in by_id["shell-git-push"]["arguments"]["command"]
