"""Offline tests of the judge agent: manifest, models, prompt, eval cases."""

import json
import subprocess
from pathlib import Path
from typing import Any

import anthropic
import pytest
import yaml
from pydantic import ValidationError

from fixtures.build import build, discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec, WeightsSpec
from runtime.runner import build_request
from runtime.scoring import DIMENSIONS
from tools import MAX_OUTPUT_BYTES, TOOLS, tool_defs
from tools._common import truncate
from tools.registry import OUTSIDE_WRITES

AGENT_DIR = AGENTS_DIR / "judge"
SEVERITIES = ["info", "minor", "major", "blocker"]


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("judge", "^2.0")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    data = yaml.safe_load((AGENT_DIR / "evals" / "cases.yaml").read_text(encoding="utf-8"))
    return data["cases"]


def _finding(**overrides: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "file": "series/rolling.py",
        "line_start": 12,
        "line_end": 13,
        "severity": "major",
        "category": "logic",
        "title": "Loop bound drops the last window",
        "body": "The loop stops one step early.",
    }
    return finding | overrides


def _review(*findings: dict[str, Any]) -> dict[str, Any]:
    verdict = "request_changes" if findings else "approve"
    return {"findings": list(findings), "summary": "s", "verdict": verdict}


def _input(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo_path": "/repo",
        "reviews": {"code-reviewer@1.2.0": _review(_finding())},
        "diff_context": {"base": "base", "head": "head", "diff": "diff --git a/x b/x\n"},
    }
    return payload | overrides


def _score(value: float) -> dict[str, Any]:
    return {"value": value, "justification": "j"}


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "scores": {
            "correctness_risk": _score(0.7),
            "maintainability": _score(0.8),
            "security_risk": _score(0.0),
            "test_coverage": _score(0.3),
        },
        "merged_findings": [_finding(sources=["code-reviewer@1.2.0", "perf-reviewer@0.3.0"])],
        "rationale": "The last window is dropped.",
    }
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "judge@2.1.0"
    assert spec.model == "claude-opus-5-5"
    assert spec.effort == "high"
    assert spec.evals.min_pass_rate == 0.75
    assert (spec.limits.max_tool_turns, spec.limits.timeout_s, spec.limits.max_output_tokens) == (
        10,
        180,
        16000,
    )


def test_scoring_weighs_every_dimension_and_sums_to_one(spec: LLMSpec) -> None:
    assert spec.scoring is not None
    weights = spec.scoring.weights.model_dump()
    assert set(weights) == set(DIMENSIONS)
    assert sum(weights.values()) == pytest.approx(1)
    assert spec.scoring.ship_threshold == 0.7


def test_the_model_s_only_tool_is_read_file(spec: LLMSpec) -> None:
    # The two outside writes are listed for the pipeline to make on the judge's behalf
    # (docs/decisions/0003-outside-writes.md); tool_defs never hands them to the model.
    assert list(spec.tools.allow) == ["git.read_file", "github.create_pr", "github.comment_pr"]
    assert not spec.tools.deny
    assert set(spec.tools.allow) <= set(TOOLS)
    assert set(spec.tools.allow) - {"git.read_file"} <= OUTSIDE_WRITES
    assert [d["name"] for d in tool_defs(spec.tools.allow)] == ["git__read_file"]


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "JudgeInput"
    assert spec.output_class.__name__ == "JudgeOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "ship_threshold" not in json.dumps(request)


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"scores", "merged_findings", "rationale"}
    assert set(schema["$defs"]["Scores"]["required"]) == set(DIMENSIONS)
    merged = schema["$defs"]["MergedFinding"]
    assert "sources" in merged["required"]
    assert merged["properties"]["severity"]["enum"] == SEVERITIES
    assert merged["additionalProperties"] is False


def test_the_scored_dimensions_are_the_weighted_ones(spec: LLMSpec) -> None:
    scores = spec.output_class.model_fields["scores"].annotation
    assert isinstance(scores, type)
    assert set(scores.model_fields) == set(WeightsSpec.model_fields) == set(DIMENSIONS)


# --- input


def test_input_takes_reviews_failures_and_the_diff(spec: LLMSpec) -> None:
    payload = _input(
        failed_reviewers=[{"reviewer": "security-reviewer@1.0.0", "error": "RunTimeout"}]
    )
    parsed = spec.input_class.model_validate(payload).model_dump()
    assert list(parsed["reviews"]) == ["code-reviewer@1.2.0"]
    assert parsed["failed_reviewers"][0]["reviewer"] == "security-reviewer@1.0.0"
    assert spec.input_class.model_validate(_input()).model_dump()["failed_reviewers"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"reviews": {}},
        {"reviews": {"code-reviewer": _review()}},
        {"reviews": {"code-reviewer@1.2": _review()}},
        {"reviews": {"Code-Reviewer@1.2.0": _review()}},
        {"reviews": {"code-reviewer@1.2.0": {"findings": [], "summary": "s"}}},
        {"failed_reviewers": [{"reviewer": "security-reviewer@1.0.0", "error": ""}]},
        {"failed_reviewers": [{"reviewer": "code-reviewer@1.2.0", "error": "boom"}]},
        {"diff_context": {"base": "base", "head": "head"}},
        {"surprise": 1},
    ],
)
def test_a_malformed_input_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(_input(**bad))


def test_a_reviewer_that_both_reviewed_and_failed_is_named(spec: LLMSpec) -> None:
    payload = _input(failed_reviewers=[{"reviewer": "code-reviewer@1.2.0", "error": "boom"}])
    with pytest.raises(ValidationError, match="code-reviewer@1.2.0 is listed more than once"):
        spec.input_class.model_validate(payload)


def test_a_diff_over_the_tool_output_limit_is_rejected(spec: LLMSpec) -> None:
    raw = "+" + "x" * MAX_OUTPUT_BYTES + "\n"
    context = {"base": "base", "head": "head", "diff": raw}
    with pytest.raises(ValidationError, match="over the 65536-byte limit"):
        spec.input_class.model_validate(_input(diff_context=context))


def test_a_diff_cut_by_the_tool_limit_is_accepted(spec: LLMSpec) -> None:
    cut = truncate(("+" + "x" * (MAX_OUTPUT_BYTES + 500) + "\n").encode(), MAX_OUTPUT_BYTES)
    assert len(cut.encode("utf-8")) > MAX_OUTPUT_BYTES
    context = {"base": "base", "head": "head", "diff": cut}
    spec.input_class.model_validate(_input(diff_context=context))


# --- output


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output())
    again = spec.output_class.model_validate_json(output.model_dump_json())
    assert again == output
    dumped = json.loads(output.model_dump_json())
    assert dumped["merged_findings"][0]["sources"] == ["code-reviewer@1.2.0", "perf-reviewer@0.3.0"]
    assert dumped["scores"]["correctness_risk"]["value"] == 0.7


def test_a_judgement_with_nothing_left_has_no_merged_findings(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output(merged_findings=[]))
    assert output.model_dump()["merged_findings"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"merged_findings": [_finding()]},
        {"merged_findings": [_finding(sources=[])]},
        {"merged_findings": [_finding(sources=["code-reviewer@1.2.0"], severity="critical")]},
        {"scores": {"correctness_risk": _score(0.5)}},
        {"scores": _output()["scores"] | {"security_risk": _score(1.5)}},
        {"verdict": "approve"},
    ],
)
def test_a_malformed_judgement_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    # Only a long enough prefix is cached; four characters a token is a floor.
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 2000


def test_prompt_keeps_the_judging_rules(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for phrase in (
        "Trust a finding only when you have confirmed the line it cites",
        "cites a line that does not exist",
        "prefer the stricter severity",
        "A failed reviewer leaves a blind spot",
        "Do not add findings of your own",
    ):
        assert phrase in text


def test_prompt_names_every_tool_it_talks_about(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        if tool in OUTSIDE_WRITES:
            # The model never holds an outside write, so its prompt does not talk about one.
            assert tool not in text
        else:
            assert f"`{tool}`" in text


def test_prompt_holds_no_gate_thresholds_or_weights(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    for word in ("min_pass_rate", "threshold", "weight", "ship"):
        assert word not in text


def test_prompt_does_not_quote_an_eval_case(spec: LLMSpec, cases: list[dict[str, Any]]) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for case in cases:
        for defect in case["expected"]["defects"]:
            assert defect["file"] not in text


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
        assert "repo_path" not in case["payload"]
        spec.input_class.model_validate({"repo_path": "/repo", **case["payload"]})
        assert case["grader"] == "llm_judge"
        assert case["expected"]["verdict"] is None
        assert case["expected"]["criteria"], case["id"]


def test_case_diffs_are_what_show_diff_returns(cases: list[dict[str, Any]], tmp_path: Path) -> None:
    built = {f.name: f for f in build(sorted({c["fixture"] for c in cases}), dest=tmp_path)}
    for case in cases:
        context = case["payload"]["diff_context"]
        repo = built[case["fixture"]].path
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-color"]
            + [context["base"], context["head"]],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert context["diff"] == diff, case["id"]


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


def test_the_cases_cover_the_four_panel_situations(cases: list[dict[str, Any]]) -> None:
    by_id = {case["id"]: case["payload"] for case in cases}
    source = discover()["off-by-one"].source / "head" / "series" / "rolling.py"
    length = len(source.read_text(encoding="utf-8").splitlines())
    unanimous = by_id["unanimous-panel"]["reviews"]
    assert {r["findings"][0]["severity"] for r in unanimous.values() if r["findings"]} == {"major"}
    disagreeing = by_id["severity-disagreement"]["reviews"]
    severities = {r["findings"][0]["severity"] for r in disagreeing.values() if r["findings"]}
    assert severities == {"major", "minor"}
    lines = [
        f["line_start"]
        for r in by_id["nonexistent-line"]["reviews"].values()
        for f in r["findings"]
    ]
    assert max(lines) > length
    assert by_id["failed-reviewer"]["failed_reviewers"]
