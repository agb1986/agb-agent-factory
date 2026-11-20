"""Offline tests of the documentation agent: manifest, models, prompt, eval cases."""

from pathlib import PurePosixPath
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from fixtures.build import discover
from pipelines.docs_check import COMMENT_SYNTAX, DOCS_GLOBS
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, ToolListError, tool_defs

AGENT_DIR = AGENTS_DIR / "documentation-agent"
TICKET: dict[str, Any] = {
    "kind": "docs",
    "title": "Document slugify",
    "body": "The change base..head added slugify to textkit/words.py.",
    "acceptance": ["slugify has a docstring and the README names it."],
    "repo": "acme/textkit",
    "ref": "head",
    "impacted_paths": ["textkit/words.py"],
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("documentation-agent", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "files_changed": [
            {"path": "README.md", "action": "modified", "summary": "slugify section"},
            {"path": "textkit/words.py", "action": "modified", "summary": "slugify docstring"},
        ],
        "commands_run": [
            {"argv": ["git", "diff", "base..head"], "exit_code": 0, "output": "+def slugify"}
        ],
        "summary": "slugify in textkit/words.py now has a docstring and a README entry.",
        "docs_changed": [
            {
                "path": "README.md",
                "what": "A slugify entry under words.",
                "why": "base..head added slugify to textkit/words.py.",
            }
        ],
        "blocked": False,
        "blocked_reason": None,
    }
    return output | overrides


def _blocked(**overrides: Any) -> dict[str, Any]:
    return (
        _output(
            files_changed=[],
            docs_changed=[],
            summary="Ran git diff base..head.",
            blocked=True,
            blocked_reason="git diff base..head shows no change.",
        )
        | overrides
    )


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "documentation-agent@0.1.0"
    assert spec.team == "dev"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "xhigh"
    assert spec.sandbox is True
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 30
    assert spec.limits.max_tokens_total is not None


def test_the_manifest_puts_the_agent_behind_the_action_guard(spec: LLMSpec) -> None:
    assert spec.guard == "action-guard@^0.1"


def test_tools_are_the_refactorers_and_need_the_sandbox_flag(spec: LLMSpec) -> None:
    refactorer = resolve("refactorer", "^0.1")
    assert isinstance(refactorer, LLMSpec)
    assert spec.tools.allow == refactorer.tools.allow
    assert set(spec.tools.allow) <= set(TOOLS)
    assert len(tool_defs(spec.tools.allow, spec.tools.deny, sandbox=spec.sandbox)) == len(
        spec.tools.allow
    )
    with pytest.raises(ToolListError):
        tool_defs(spec.tools.allow, spec.tools.deny)


def test_the_files_policy_matches_the_docs_check(spec: LLMSpec) -> None:
    assert spec.files is not None
    expected = {*DOCS_GLOBS, "**/*.py", *(f"**/*{suffix}" for suffix in COMMENT_SYNTAX)}
    assert set(spec.files.allow) == expected


def test_the_files_policy_denies_what_the_refactorers_does(spec: LLMSpec) -> None:
    refactorer = resolve("refactorer", "^0.1")
    assert isinstance(refactorer, LLMSpec)
    assert spec.files is not None and refactorer.files is not None
    assert spec.files.deny == refactorer.files.deny


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/api.md",
        "docs/guide/start.md",
        "pkg/NOTES.md",
        "textkit/words.py",
        "scripts/run.sh",
        "config/app.yaml",
        "web/app.ts",
    ],
)
def test_the_files_policy_allows_docs_and_commentable_source(spec: LLMSpec, path: str) -> None:
    assert spec.files is not None
    assert spec.files.denial(path) is None, path


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_words.py",
        "pkg/tests/helpers.py",
        "conftest.py",
        "test_words.py",
        "pkg/words_test.py",
        ".git/config",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "pytest/__init__.py",
        "_pytest.py",
        "data/table.csv",
        "web/index.html",
        "Makefile",
        "logo.png",
    ],
)
def test_the_files_policy_refuses_tests_vcs_ci_and_unchecked_types(
    spec: LLMSpec, path: str
) -> None:
    assert spec.files is not None
    assert spec.files.denial(path) is not None, path


@pytest.mark.parametrize("path", ["README.md", "docs/api.md", "textkit/words.py", "run.sh"])
def test_every_allowed_type_is_one_the_docs_check_reads(spec: LLMSpec, path: str) -> None:
    assert spec.files is not None
    assert spec.files.denial(path) is None
    pure = PurePosixPath(path)
    assert any(pure.full_match(glob) for glob in DOCS_GLOBS) or pure.suffix in {
        ".py",
        *COMMENT_SYNTAX,
    }


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "DocumentationAgentInput"
    assert spec.output_class.__name__ == "DocumentationAgentOutput"


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
        "docs_changed",
        "blocked",
        "blocked_reason",
    }


# --- models


def test_input_is_what_every_coder_takes_with_a_docs_ticket(spec: LLMSpec) -> None:
    payload = {"repo_path": "/repo", "ticket": TICKET}
    parsed = spec.input_class.model_validate(payload).model_dump()
    assert set(parsed) == {"repo_path", "ticket", "depends_on"}
    assert parsed["depends_on"] == []
    assert parsed["ticket"]["impacted_paths"] == ["textkit/words.py"]
    for field in ("status", "cost_usd", "id", "work_item_id"):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(payload | {field: "x"})
    with pytest.raises(ValidationError):
        spec.input_class.model_validate({"repo_path": "/repo"})


@pytest.mark.parametrize("kind", ["feature", "bug", "refactor"])
def test_input_refuses_a_ticket_of_another_kind(spec: LLMSpec, kind: str) -> None:
    with pytest.raises(ValidationError, match="docs tickets"):
        spec.input_class.model_validate({"repo_path": "/repo", "ticket": TICKET | {"kind": kind}})


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    for answer in (_output(), _blocked()):
        output = spec.output_class.model_validate(answer)
        assert spec.output_class.model_validate_json(output.model_dump_json()) == output


def test_output_has_the_fields_the_build_loop_reads(spec: LLMSpec) -> None:
    answer = spec.output_class.model_validate(_output()).model_dump(mode="json")
    assert {"files_changed", "summary", "blocked", "blocked_reason"} <= set(answer)


@pytest.mark.parametrize(
    "bad",
    [
        {"blocked_reason": None},
        {"blocked_reason": "  "},
        {"files_changed": [{"path": "README.md", "action": "modified", "summary": "partial"}]},
        {"docs_changed": [{"path": "README.md", "what": "a line", "why": "a change"}]},
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
        {"files_changed": [{"path": "a.md", "action": "renamed", "summary": "s"}]},
        {"commands_run": [{"argv": ["git"], "exit_code": "zero", "output": ""}]},
        {"docs_changed": [{"path": "README.md", "what": "a line"}]},
        {"docs_changed": "README.md"},
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
    for field in ("path", "what", "why"):
        assert f"`{field}`" in text, field
    assert "you change no code" in text
    assert "only docstrings and comments" in text
    assert "never delete or rename a file" in text


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
        assert "base..head" in case.payload["ticket"]["body"]


def test_every_case_is_graded_by_a_rubric_on_the_answer(spec: LLMSpec, cases: list[Case]) -> None:
    fields = set(spec.output_class.model_fields)
    for case in cases:
        rubric = case.expected.rubric
        assert rubric is not None, case.id
        assert {"summary", "docs_changed"} <= set(rubric.required_fields)
        assert set(rubric.required_fields) | set(rubric.empty_fields) <= fields
        assert any(criterion.required for criterion in rubric.criteria)


def test_every_case_runs_the_drift_check_over_the_changed_files(cases: list[Case]) -> None:
    for case in cases:
        run = case.expected.run
        assert run is not None
        assert (run.suite, run.tests) == ("pass", "unchanged")
        assert run.doc_drift is not None
        assert set(case.payload["ticket"]["impacted_paths"]) <= set(run.doc_drift.paths)
        assert run.doc_drift.docs == ("README.md", "docs/**/*.md")


def test_the_cases_cover_the_four_docs_fixtures(cases: list[Case]) -> None:
    assert {case.fixture for case in cases} == {
        "docs-new-function",
        "docs-new-class",
        "docs-renamed-function",
        "docs-new-module",
    }
