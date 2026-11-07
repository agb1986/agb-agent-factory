"""Offline tests of the security-reviewer agent: manifest, models, prompt, eval cases."""

import json
from pathlib import Path
from typing import Any

import anthropic
import pytest
import yaml
from pydantic import ValidationError

from fixtures.build import discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, tool_defs

AGENT_DIR = AGENTS_DIR / "security-reviewer"
SEVERITIES = ["info", "minor", "major", "blocker"]
CATEGORIES = [
    "sql-injection",
    "command-injection",
    "path-traversal",
    "missing-authorization",
    "hardcoded-secret",
    "unsafe-deserialization",
    "vulnerable-dependency",
    "weak-randomness",
    "other-security",
]


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("security-reviewer", "^1.0")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    data = yaml.safe_load((AGENT_DIR / "evals" / "cases.yaml").read_text(encoding="utf-8"))
    return data["cases"]


def _finding(**overrides: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "file": "accounts/db.py",
        "line_start": 12,
        "line_end": 13,
        "severity": "major",
        "category": "sql-injection",
        "title": "SQL injection through the name argument",
        "body": "The name is formatted into the query.",
    }
    return finding | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "security-reviewer@1.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "high"
    assert spec.evals.min_pass_rate == 0.75
    assert (spec.limits.max_tool_turns, spec.limits.timeout_s, spec.limits.max_output_tokens) == (
        12,
        180,
        4096,
    )


def test_tools_are_the_four_read_only_ones_and_all_exist(spec: LLMSpec) -> None:
    assert list(spec.tools.allow) == ["git.read_file", "git.show_diff", "rg.search", "sast.scan"]
    assert not spec.tools.deny
    assert set(spec.tools.allow) <= set(TOOLS)
    assert len(tool_defs(spec.tools.allow)) == 4


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "ReviewInput"
    assert spec.output_class.__name__ == "ReviewOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"findings", "summary", "verdict"}
    finding = schema["$defs"]["Finding"]
    assert finding["properties"]["severity"]["enum"] == SEVERITIES
    assert finding["additionalProperties"] is False


# --- models


def test_input_requires_the_revisions_and_focus_is_optional(spec: LLMSpec) -> None:
    payload = {"repo_path": "/repo", "base": "base", "head": "head"}
    parsed = spec.input_class.model_validate(payload)
    assert parsed.model_dump()["focus"] is None
    with pytest.raises(ValidationError):
        spec.input_class.model_validate({"repo_path": "/repo", "base": "base"})
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(payload | {"surprise": 1})


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(
        {
            "findings": [_finding(suggested_fix="Bind name as a query parameter.")],
            "summary": "Adds a user lookup; the name is formatted into SQL.",
            "verdict": "request_changes",
        }
    )
    again = spec.output_class.model_validate_json(output.model_dump_json())
    assert again == output
    assert json.loads(output.model_dump_json())["findings"][0]["line_start"] == 12


def test_a_clean_review_has_no_findings(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(
        {
            "findings": [],
            "summary": "Pure rename; no security-relevant change.",
            "verdict": "approve",
        }
    )
    assert output.model_dump()["findings"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"severity": "critical"},
        {"line_start": 0},
        {"line_start": 9, "line_end": 3},
        {"extra": "field"},
    ],
)
def test_a_malformed_finding_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(
            {"findings": [_finding(**bad)], "summary": "s", "verdict": "approve"}
        )


def test_an_unknown_verdict_is_rejected(spec: LLMSpec) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate({"findings": [], "summary": "s", "verdict": "maybe"})


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    # Sonnet 5.5 caches prefixes of 512 tokens or more; four characters a token is a floor.
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 2000


def test_prompt_keeps_the_review_discipline(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for phrase in (
        "Read the diff first",
        "head revision",
        "Never invent a line you have not seen",
        "A review never depends on `rg.search`",
    ):
        assert phrase in text


def test_prompt_covers_the_security_checklist(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    for topic in (
        "sql injection",
        "command injection",
        "missing authorisation",
        "hard-coded credentials",
        "unsafe deserialisation",
        "dependency pins with known advisories",
        "path traversal",
        "weak randomness",
    ):
        assert topic in text


def test_prompt_fixes_the_category_strings(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for category in CATEGORIES:
        assert f"`{category}`" in text


def test_prompt_sends_the_model_to_rg_for_call_sites(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "find the other call sites" in text
    assert "before you decide whether a sink is reachable" in text


def test_prompt_takes_line_numbers_from_rg_when_it_can(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "take the line from the `path:line:text` result" in text
    assert "count by hand" in text


def test_prompt_makes_the_reviewer_verify_scanner_findings(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "Verify every `sast.scan` finding against the code before you report it" in text
    assert "never copy a tool finding into a review" in text
    assert "A review never depends on `sast.scan`" in text


def test_the_planted_categories_are_in_the_fixed_list() -> None:
    fixtures = discover()
    for name in ("hardcoded-secret", "path-traversal", "secret-in-config"):
        for defect in fixtures[name].load_manifest().defects:
            assert defect.category in CATEGORIES


def test_prompt_names_every_tool_it_talks_about(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_at_least_four_cases_with_unique_ids(cases: list[dict[str, Any]]) -> None:
    ids = [case["id"] for case in cases]
    assert len(ids) >= 4
    assert len(set(ids)) == len(ids)


def test_cases_reference_existing_fixtures(cases: list[dict[str, Any]]) -> None:
    fixtures = discover()
    for case in cases:
        assert case["fixture"] in fixtures, case["id"]


def test_case_payloads_complete_into_valid_inputs(
    spec: LLMSpec, cases: list[dict[str, Any]]
) -> None:
    for case in cases:
        spec.input_class.model_validate({"repo_path": "/repo", **case["payload"]})
        assert case["grader"] == "llm_judge"
        assert case["expected"]["verdict"] in {"approve", "request_changes"}


def test_expected_defects_match_the_fixture_manifests(cases: list[dict[str, Any]]) -> None:
    fixtures = discover()
    for case in cases:
        planted = {d.id: d for d in fixtures[case["fixture"]].load_manifest().defects}
        expected = case["expected"]["defects"]
        assert {d["id"] for d in expected} == set(planted), case["id"]
        for want in expected:
            defect = planted[want["id"]]
            assert want["file"] == defect.file
            assert (want["line_start"], want["line_end"]) == (defect.line_start, defect.line_end)
            assert want["severity_floor"] in SEVERITIES
            assert SEVERITIES.index(want["severity_floor"]) <= SEVERITIES.index(defect.severity)


def test_the_cases_cover_clean_and_defective_changes(cases: list[dict[str, Any]]) -> None:
    verdicts = {case["expected"]["verdict"] for case in cases}
    assert verdicts == {"approve", "request_changes"}
    assert any(not case["expected"]["defects"] for case in cases)
    assert Path(AGENT_DIR / "evals" / "cases.yaml").is_file()


def test_the_scanner_cases_cover_a_true_and_a_false_positive(cases: list[dict[str, Any]]) -> None:
    by_id = {case["id"]: case for case in cases}
    assert by_id["secret-in-config"]["expected"]["verdict"] == "request_changes"
    false_positive = by_id["sast-false-positive"]["expected"]
    assert (false_positive["verdict"], false_positive["defects"]) == ("approve", [])
    assert "TOKEN_HEADER" in false_positive["criteria"]
