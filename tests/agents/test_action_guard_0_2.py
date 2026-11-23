"""Offline tests of action-guard 0.2: the `sandbox` field and the rules for the spike profile."""

from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.guard import render_call
from runtime.hooks import ToolCall

AGENT_DIR = AGENTS_DIR / "action-guard" / "0.2.1"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("action-guard", "^0.2")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _input(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": "shell.run",
        "arguments": {"argv": ["pip", "install", "requests"]},
        "worktree_root": "/tmp/factory-spike-x",
        "task_summary": "Learn the shape of an API.",
    }
    return payload | overrides


def test_manifest_resolves_to_0_2_with_the_same_model(spec: LLMSpec) -> None:
    assert spec.address == "action-guard@0.2.1"
    assert spec.model == "claude-haiku-5-5"
    assert spec.effort == "low"
    assert spec.guard is None
    assert not spec.tools.allow
    assert spec.sandbox is False


def test_the_0_1_guard_is_still_resolved_by_its_range() -> None:
    assert resolve("action-guard", "^0.1").address.startswith("action-guard@0.1.")


def test_sandbox_defaults_to_offline(spec: LLMSpec) -> None:
    assert spec.input_class.model_validate(_input()).model_dump()["sandbox"] == "offline"
    assert spec.input_class.model_validate(_input(sandbox="spike")).model_dump()["sandbox"] == (
        "spike"
    )
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(_input(sandbox="online"))


def test_the_rendered_call_of_either_profile_is_a_valid_input(spec: LLMSpec) -> None:
    call = ToolCall("shell.run", {"argv": ["python", "probe.py"]}, "spike@0.1.0")
    for sandbox in (None, "offline", "spike"):
        rendered = render_call(call, worktree_root="/w", task_summary="t", sandbox=sandbox)
        spec.input_class.model_validate(rendered)


def test_the_prompt_names_the_spike_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for phrase in ("`sandbox`", "`spike`", "`offline`", "169.254.169.254", "/work"):
        assert phrase in text, phrase


def test_the_cases_keep_the_0_1_ones_and_add_spike_ones(spec: LLMSpec, cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(set(ids)) == len(ids)
    old = load_cases(AGENTS_DIR / "action-guard" / "evals" / "cases.yaml")
    assert {case.id for case in old} <= set(ids)
    spike = [case for case in cases if case.payload.get("sandbox") == "spike"]
    assert len(spike) >= 5
    for case in cases:
        spec.input_class.model_validate(case.payload)
