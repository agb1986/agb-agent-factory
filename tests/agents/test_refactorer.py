"""Offline tests of the refactorer agent: manifest, models, prompt, eval cases."""

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

AGENT_DIR = AGENTS_DIR / "refactorer"
TICKET: dict[str, Any] = {
    "kind": "refactor",
    "title": "The three line formatters repeat the same amount code",
    "body": "Move the amount check and formatting into one private helper.",
    "acceptance": ["The three functions return the same text as before."],
    "repo": "acme/report",
    "ref": "head",
    "impacted_paths": ["report/lines.py"],
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("refactorer", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "files_changed": [
            {"path": "report/lines.py", "action": "modified", "summary": "one amount helper"}
        ],
        "commands_run": [
            {"argv": ["python", "-m", "pytest", "-q"], "exit_code": 0, "output": "9 passed"}
        ],
        "summary": "invoice_line, refund_line and credit_line call one private _amount helper.",
        "behaviour_kept": "Public names and signatures unchanged; 9 passed before and after.",
        "signatures_changed": [],
        "blocked": False,
        "blocked_reason": None,
    }
    return output | overrides


def _blocked(**overrides: Any) -> dict[str, Any]:
    return (
        _output(
            files_changed=[],
            summary="Ran the suite before reading anything.",
            behaviour_kept="Nothing changed.",
            blocked=True,
            blocked_reason="tests/test_lines.py::test_credit_line fails before any change.",
        )
        | overrides
    )


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "refactorer@0.1.0"
    assert spec.team == "dev"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "xhigh"
    assert spec.sandbox is True
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 30
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
    "path", ["report/lines.py", "report/new_helper.py", "docs/usage.md", "lib/pyproject.toml"]
)
def test_the_files_policy_allows_production_code(spec: LLMSpec, path: str) -> None:
    assert spec.files is not None
    assert spec.files.denial(path) is None, path


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_lines.py",
        "report/tests/test_lines.py",
        "conftest.py",
        "report/conftest.py",
        "test_lines.py",
        "report/lines_test.py",
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
def test_the_files_policy_refuses_tests_vcs_ci_and_the_test_run(spec: LLMSpec, path: str) -> None:
    assert spec.files is not None
    assert spec.files.denial(path) is not None, path


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "RefactorerInput"
    assert spec.output_class.__name__ == "RefactorerOutput"


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
        "summary",
        "behaviour_kept",
        "signatures_changed",
        "blocked",
    }


# --- models


def test_input_carries_a_refactor_ticket_and_no_store_fields(spec: LLMSpec) -> None:
    payload = {"repo_path": "/repo", "ticket": TICKET}
    parsed = spec.input_class.model_validate(payload).model_dump()
    assert parsed["depends_on"] == []
    assert parsed["ticket"]["impacted_paths"] == ["report/lines.py"]
    for field in ("status", "cost_usd", "id", "work_item_id"):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(payload | {field: "x"})
    with pytest.raises(ValidationError):
        spec.input_class.model_validate({"repo_path": "/repo"})


@pytest.mark.parametrize("kind", ["feature", "bug", "docs"])
def test_input_refuses_a_ticket_of_another_kind(spec: LLMSpec, kind: str) -> None:
    with pytest.raises(ValidationError, match="refactor tickets"):
        spec.input_class.model_validate({"repo_path": "/repo", "ticket": TICKET | {"kind": kind}})


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    for answer in (_output(), _blocked()):
        output = spec.output_class.model_validate(answer)
        assert spec.output_class.model_validate_json(output.model_dump_json()) == output


@pytest.mark.parametrize(
    "bad",
    [
        {"blocked_reason": None},
        {"blocked_reason": "  "},
        {
            "files_changed": [
                {"path": "report/lines.py", "action": "modified", "summary": "partial"}
            ]
        },
    ],
)
def test_a_blocked_answer_has_a_reason_and_changes_no_file(
    spec: LLMSpec, bad: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_blocked(**bad))


def test_an_unblocked_answer_has_no_reason(spec: LLMSpec) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(blocked_reason="why"))


@pytest.mark.parametrize(
    "bad",
    [
        {"files_changed": [{"path": "a.py", "action": "renamed", "summary": "s"}]},
        {"commands_run": [{"argv": ["pytest"], "exit_code": "zero", "output": ""}]},
        {"signatures_changed": "report/lines.py::credit_line"},
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
    assert "you never change a test file" in text
    assert "must be private" in text
    assert "`signatures_changed`" in text


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
        assert case.expected.rubric is None
        assert case.expected.criteria


def test_every_case_pins_the_suite_the_tests_and_the_targets_public_api(
    cases: list[Case],
) -> None:
    for case in cases:
        run = case.expected.run
        assert run is not None
        assert (run.suite, run.production, run.tests) == ("pass", "changed", "unchanged")
        assert run.public_api is not None
        assert set(run.public_api) == set(case.payload["ticket"]["impacted_paths"]), case.id


def test_the_cases_cover_the_four_planted_smells(cases: list[Case]) -> None:
    assert {case.fixture for case in cases} == {
        "n-plus-one",
        "refactor-duplicate",
        "refactor-deprecated",
        "refactor-magic-numbers",
    }
