"""Offline tests of the benchmark agent: manifest, models, prompt, eval cases."""

import types
import typing
from typing import Any

import anthropic
import pytest
from pydantic import BaseModel, ValidationError

from evals.cases import Case, load_cases
from fixtures.build import discover
from registry.common_schemas import Claim
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, ToolListError, tool_defs

AGENT_DIR = AGENTS_DIR / "benchmark"
CITATION = {
    "url": "https://pypi.org/project/polars/",
    "quote": "Polars is licensed under the MIT License (SPDX: MIT).",
    "retrieved_at": "2026-11-07T12:00:00Z",
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("benchmark", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _cell(**overrides: Any) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "text": "polars is MIT licensed.",
        "fact": True,
        "citations": [CITATION],
        "candidate": "polars",
        "criterion": "licence",
    }
    return cell | overrides


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "candidates": [
            {"name": "polars", "url": "https://pypi.org/project/polars/", "found_by": "given",
             "status": "recommended", "ruled_out_because": None},
            {"name": "pandas", "url": None, "found_by": "search", "status": "ruled_out",
             "ruled_out_because": "No Rust API."},
        ],
        "table": [
            _cell(),
            _cell(
                text="No throughput figure is published on the PyPI page.",
                fact=False,
                citations=[],
                criterion="performance",
            ),
        ],
        "recommendation": "Use polars: it is the only candidate usable from Python and Rust.",
        "depends_on": ["The service will be ported to Rust."],
        "spike": None,
    }  # fmt: skip
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "benchmark@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "high"
    assert spec.sandbox is False
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tokens_total is not None


def test_tools_are_the_web_and_read_only_ones(spec: LLMSpec) -> None:
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
    with pytest.raises(ToolListError, match="allow-list"):
        tool_defs(spec.tools.allow, spec.tools.deny)


def test_web_allow_list_holds_the_stable_hosts(spec: LLMSpec) -> None:
    for host in (
        "pypi.org",
        "github.com",
        "docs.python.org",
        "peps.python.org",
        "opensource.org",
        "spdx.org",
        "choosealicense.com",
    ):
        assert host in spec.tools.web.allow_domains


def test_web_tools_keep_the_agent_out_of_the_sandbox(spec: LLMSpec) -> None:
    with pytest.raises(ToolListError):
        tool_defs(
            spec.tools.allow,
            spec.tools.deny,
            sandbox=True,
            web_domains=spec.tools.web.allow_domains,
        )


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "BenchmarkInput"
    assert spec.output_class.__name__ == "BenchmarkOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert request["model"] == "claude-sonnet-5-5"
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert set(schema["required"]) == {
        "candidates",
        "table",
        "recommendation",
        "depends_on",
        "spike",
    }


# --- models


def test_input_needs_a_need_and_defaults_the_rest(spec: LLMSpec) -> None:
    parsed = spec.input_class.model_validate({"repo_path": "/repo", "need": "parse TOML"})
    assert parsed.model_dump()["candidates"] == []
    assert parsed.model_dump()["ref"] == "HEAD"
    for bad in (
        {"repo_path": "/repo"},
        {"repo_path": "/repo", "need": ""},
        {"repo_path": "/repo", "need": "x", "candidates": [{"url": "https://pypi.org/"}]},
        {"repo_path": "/repo", "need": "x", "candidates": [{"name": "a", "url": "pypi.org"}]},
        {"repo_path": "/repo", "need": "x", "extra": 1},
    ):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(bad)


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output())
    assert spec.output_class.model_validate_json(output.model_dump_json()) == output


def test_a_spike_has_a_name_and_what_it_measures(spec: LLMSpec) -> None:
    spike = {"name": "throughput at 5,000", "measures": "requests per second of both"}
    output = spec.output_class.model_validate(_output(spike=spike))
    assert output.model_dump()["spike"] == spike
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(spike={"name": "x"}))


def test_a_fact_without_a_citation_is_rejected(spec: LLMSpec) -> None:
    with pytest.raises(ValidationError, match="citation"):
        spec.output_class.model_validate(_output(table=[_cell(citations=[])]))


@pytest.mark.parametrize(
    "bad",
    [
        {"table": [_cell(criterion="stars")]},
        {"table": [_cell(candidate="numpy")]},
        {"table": [_cell(citations=[CITATION | {"url": "pypi.org/project/polars"}])]},
        {"table": [_cell(citations=[CITATION | {"quote": ""}])]},
        {"table": [_cell(score=3)]},
        {"recommendation": ""},
        {"candidates": [
            {"name": "polars", "found_by": "given", "status": "ruled_out"},
            {"name": "pandas", "found_by": "given", "status": "viable"},
        ]},
        {"candidates": [
            {"name": "polars", "found_by": "given", "status": "recommended"},
            {"name": "pandas", "found_by": "given", "status": "recommended"},
        ]},
        {"surprise": 1},
    ],
)  # fmt: skip
def test_a_malformed_output_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


def _leaf_types(annotation: Any) -> set[Any]:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return {type(value) for value in typing.get_args(annotation)}
    if origin in (list, tuple, typing.Union, types.UnionType):
        return set().union(*(_leaf_types(arg) for arg in typing.get_args(annotation)))
    return {annotation}


def _number_fields(model: type[BaseModel], path: str = "") -> list[str]:
    found: list[str] = []
    for name, field in model.model_fields.items():
        for leaf in _leaf_types(field.annotation):
            if isinstance(leaf, type) and issubclass(leaf, BaseModel):
                found += _number_fields(leaf, f"{path}{name}.")
            elif leaf is not bool and isinstance(leaf, type) and issubclass(leaf, (int, float)):
                found.append(f"{path}{name}")
    return found


def test_every_number_bearing_field_is_a_claim(spec: LLMSpec) -> None:
    # No output field has a number type, so a figure can only appear as text, and the table, the
    # one place the prompt allows figures, holds claims that a citation must back.
    assert _number_fields(spec.output_class) == []
    table = spec.output_class.model_fields["table"].annotation
    (cell,) = typing.get_args(table)
    assert issubclass(cell, Claim)


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 500


def test_prompt_names_every_tool_field_and_rule(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    for field in spec.output_class.model_fields:
        assert f"`{field}`" in text
    for rule in (
        "numbers come only from citations",
        'Prefer "not published" to a guess',
        "<<<BEGIN FETCHED CONTENT>>>",
        "Never invent a URL",
        "never a reason to name libraries from memory",
    ):
        assert rule in text


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
        parsed = spec.input_class.model_validate({"repo_path": "/repo", **case.payload})
        # web.search is not relied on: every case names its candidates.
        assert parsed.model_dump()["candidates"]
        assert case.grader == "rubric"
        assert case.expected.verdict is None
        assert case.expected.rubric is not None
        assert any(criterion.required for criterion in case.expected.rubric.criteria)


def test_every_case_checks_citations_of_the_table(cases: list[Case]) -> None:
    for case in cases:
        check = case.expected.citations_resolve
        assert check is not None, case.id
        assert check.claims == "table"


def test_case_domains_are_a_subset_of_the_manifest(spec: LLMSpec, cases: list[Case]) -> None:
    manifest = set(spec.tools.web.allow_domains)
    for case in cases:
        check = case.expected.citations_resolve
        assert check is not None
        assert set(check.allow_domains) <= manifest, case.id


def test_the_cases_cover_a_spike_and_a_licence_rule_out(cases: list[Case]) -> None:
    rubrics = {case.id: case.expected.rubric for case in cases}
    with_spike = [
        case_id
        for case_id, rubric in rubrics.items()
        if rubric and "spike.name" in rubric.required_fields
    ]
    without_spike = [
        case_id for case_id, rubric in rubrics.items() if rubric and "spike" in rubric.empty_fields
    ]
    assert with_spike == ["http-client-throughput-spike"]
    assert len(without_spike) == len(cases) - 1
    licence = rubrics["pdf-agpl-ruled-out"]
    assert licence is not None
    assert any("AGPL" in criterion.text for criterion in licence.criteria)
