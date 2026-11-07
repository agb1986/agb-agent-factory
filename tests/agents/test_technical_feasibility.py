"""Offline tests of the technical-feasibility agent: manifest, models, prompt, eval cases."""

from typing import Any, get_args

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from fixtures.build import discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, ToolListError, tool_defs

AGENT_DIR = AGENTS_DIR / "technical-feasibility"
VERDICTS = ("viable", "viable_with_constraints", "not_viable", "unknown")
DOMAINS = {
    "docs.python.org",
    "peps.python.org",
    "pypi.org",
    "github.com",
    "developer.mozilla.org",
    "docs.djangoproject.com",
    "docs.sqlalchemy.org",
    "fastapi.tiangolo.com",
    "redis.io",
    "www.postgresql.org",
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("technical-feasibility", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _citation(**overrides: Any) -> dict[str, Any]:
    citation: dict[str, Any] = {
        "url": "https://docs.python.org/3/library/tomllib.html",
        "quote": "This module does not support writing TOML.",
        "retrieved_at": "2026-11-07T12:00:00Z",
    }
    return citation | overrides


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "verdict": "not_viable",
        "summary": "The standard library can read TOML but cannot write it.",
        "reasons": [
            {"text": "tomllib cannot write TOML.", "fact": True, "citations": [_citation()]},
            {"text": "A writer package would be needed.", "fact": False, "citations": []},
        ],
        "constraints": [],
        "spike": None,
    }
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "technical-feasibility@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "high"
    assert spec.sandbox is False
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 10
    assert spec.limits.max_tokens_total is not None


def test_tools_are_the_five_read_only_ones(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) == {
        "web.fetch",
        "web.search",
        "memory.search",
        "git.read_file",
        "rg.search",
    }
    assert set(spec.tools.allow) <= set(TOOLS)
    defs = tool_defs(spec.tools.allow, spec.tools.deny, web_domains=spec.tools.web.allow_domains)
    assert len(defs) == len(spec.tools.allow)


def test_the_web_allow_list_is_the_documentation_hosts(spec: LLMSpec) -> None:
    assert set(spec.tools.web.allow_domains) == DOMAINS
    with pytest.raises(ToolListError):
        tool_defs(spec.tools.allow, spec.tools.deny)


def test_web_and_memory_keep_the_agent_out_of_the_sandbox(spec: LLMSpec) -> None:
    with pytest.raises(ToolListError):
        tool_defs(
            spec.tools.allow,
            spec.tools.deny,
            sandbox=True,
            web_domains=spec.tools.web.allow_domains,
        )


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "FeasibilityInput"
    assert spec.output_class.__name__ == "FeasibilityOutput"


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
    assert set(schema["required"]) == {"verdict", "summary", "reasons", "constraints", "spike"}


# --- models


def test_input_needs_a_repo_and_a_question_and_the_ref_is_optional(spec: LLMSpec) -> None:
    parsed = spec.input_class.model_validate({"repo_path": "/repo", "question": "x"})
    assert parsed.model_dump()["ref"] is None
    spec.input_class.model_validate({"repo_path": "/repo", "ref": "head", "question": "x"})
    for bad in (
        {"repo_path": "/repo"},
        {"repo_path": "/repo", "question": ""},
        {"repo_path": "/repo", "ref": "", "question": "x"},
        {"repo_path": "/repo", "question": "x", "extra": 1},
    ):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(bad)


def test_the_verdict_is_one_of_four(spec: LLMSpec) -> None:
    assert get_args(spec.output_class.model_fields["verdict"].annotation) == VERDICTS
    for verdict in VERDICTS:
        spec.output_class.model_validate(_output(verdict=verdict))
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(verdict="maybe"))


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    spike = {"name": "time tomllib", "establishes": "Whether 200 MB parses in under a second."}
    for output in (_output(), _output(verdict="unknown", spike=spike)):
        parsed = spec.output_class.model_validate(output)
        assert spec.output_class.model_validate_json(parsed.model_dump_json()) == parsed


def test_a_fact_without_a_citation_is_rejected(spec: LLMSpec) -> None:
    claim = {"text": "tomllib cannot write TOML.", "fact": True, "citations": []}
    with pytest.raises(ValidationError, match="needs at least one citation"):
        spec.output_class.model_validate(_output(reasons=[claim]))


@pytest.mark.parametrize(
    "bad",
    [
        {"summary": ""},
        {"reasons": ["tomllib cannot write TOML."]},
        {"reasons": [{"text": "x", "fact": True, "citations": [_citation(url="docs.python.org")]}]},
        {"reasons": [{"text": "x", "fact": True, "citations": [_citation(quote="")]}]},
        {"spike": {"name": "time it"}},
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


def test_prompt_names_every_tool_field_verdict_and_host(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    for field in spec.output_class.model_fields:
        assert f"`{field}`" in text
    for verdict in VERDICTS:
        assert f"`{verdict}`" in text
    for domain in spec.tools.web.allow_domains:
        assert domain in text


def test_prompt_states_the_research_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "Prefer `unknown` to a guess" in text
    assert "word for word" in text
    assert "<<<BEGIN FETCHED CONTENT>>>" in text
    assert "Never invent a URL" in text
    assert "never guess instead" in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_four_cases_one_per_verdict(cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(set(ids)) == len(ids) == 4
    assert sorted(case.expected.verdict or "" for case in cases) == sorted(VERDICTS)


def test_cases_reference_existing_fixtures_and_complete_into_inputs(
    spec: LLMSpec, cases: list[Case]
) -> None:
    fixtures = discover()
    for case in cases:
        assert case.fixture in fixtures
        spec.input_class.model_validate({"repo_path": "/repo", **case.payload})
        assert "repo_path" not in case.payload
        assert case.grader == "rubric"
        assert case.expected.rubric is not None
        assert any(criterion.required for criterion in case.expected.rubric.criteria)


def test_every_case_checks_citations_within_the_manifest_allow_list(
    spec: LLMSpec, cases: list[Case]
) -> None:
    for case in cases:
        check = case.expected.citations_resolve
        assert check is not None, case.id
        assert check.claims == "reasons"
        assert set(check.allow_domains) <= set(spec.tools.web.allow_domains), case.id


def test_the_unknown_case_asks_for_a_spike(cases: list[Case]) -> None:
    (unknown,) = [case for case in cases if case.expected.verdict == "unknown"]
    assert unknown.expected.rubric is not None
    assert "spike" in unknown.expected.rubric.required_fields
