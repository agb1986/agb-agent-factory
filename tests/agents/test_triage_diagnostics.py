"""Offline tests of the triage-diagnostics agent: manifest, models, prompt, eval cases."""

from pathlib import Path
from typing import Any

import anthropic
import pytest
import yaml
from pydantic import ValidationError

from evals.cases import Case, load_cases
from fixtures.build import build, discover
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tools import TOOLS, tool_defs

AGENT_DIR = AGENTS_DIR / "triage-diagnostics"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("triage-diagnostics", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": "4501",
        "short_id": "SHOP-7A",
        "title": "ZeroDivisionError: division by zero",
        "culprit": "reviews.scores in average_rating",
        "level": "error",
        "count": 214,
        "release": "head",
        "exceptions": [
            {
                "type": "ZeroDivisionError",
                "value": "division by zero",
                "frames": [
                    {"file": "/app/reviews/scores.py", "function": "average_rating",
                     "line": 7, "in_app": True},
                ],
            }
        ],
        "breadcrumbs": [{"category": "reviews", "message": "loaded 0 ratings"}],
    }  # fmt: skip
    return event | overrides


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "title": "ZeroDivisionError in average_rating for a product with no ratings",
        "summary": "average_rating divides by the number of ratings, which is 0.",
        "reproduction_steps": ["Call average_rating with an empty list."],
        "suspected_paths": ["reviews/scores.py", "reviews/listing.py"],
        "culprit": {"file": "reviews/scores.py", "line_start": 6, "line_end": 7,
                    "reason": "no check for an empty list"},
        "confidence": 0.8,
        "reproduction_test": {"path": "tests/test_no_ratings.py",
                              "code": "def test_x() -> None:\n    assert False\n"},
    }  # fmt: skip
    return output | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "triage-diagnostics@0.1.0"
    assert spec.team == "sre"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "high"
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 15
    assert spec.limits.max_tokens_total is not None


def test_the_agent_is_read_only(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) == {"git.read_file", "rg.search", "deps.graph", "deps.impact"}
    assert set(spec.tools.allow) <= set(TOOLS)
    assert len(tool_defs(spec.tools.allow, spec.tools.deny)) == len(spec.tools.allow)
    # No sandbox, no files policy and no writing tools: the incident pipeline files the ticket.
    assert spec.sandbox is False
    assert spec.sandbox_profile is None
    assert spec.files is None
    assert not any(
        name.startswith(("work", "ticket", "github", "sentry", "fs.")) for name in spec.tools.allow
    )


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "TriageDiagnosticsInput"
    assert spec.output_class.__name__ == "TriageDiagnosticsOutput"


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
    assert set(schema["required"]) == set(spec.output_class.model_fields)


# --- input


def test_input_takes_an_event_a_repository_and_a_ref(spec: LLMSpec) -> None:
    good = {"repo_path": "/repo", "event": _event(), "ref": "head"}
    parsed = spec.input_class.model_validate(good)
    assert spec.input_class.model_validate_json(parsed.model_dump_json()) == parsed
    for bad in (
        {key: value for key, value in good.items() if key != "event"},
        good | {"ref": ""},
        good | {"extra": 1},
        good | {"event": _event(exceptions=[])},
        good | {"event": _event(title="")},
        good | {"event": _event(count=-1)},
        good | {"event": _event(tags={})},
    ):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(bad)


def test_a_saved_read_issue_document_is_valid_input(spec: LLMSpec) -> None:
    """The fields of the event model are the keys `sentry.read_issue` returns."""
    event = spec.input_class.model_fields["event"].annotation
    assert event is not None
    keys = set(event.model_fields)  # ty: ignore[unresolved-attribute]
    assert {"id", "short_id", "title", "release", "exceptions", "breadcrumbs"} <= keys


# --- output


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = spec.output_class.model_validate(_output())
    assert spec.output_class.model_validate_json(output.model_dump_json()) == output


@pytest.mark.parametrize(
    "bad",
    [
        {"title": ""},
        {"title": "two\nlines"},
        {"title": "x" * 121},
        {"summary": ""},
        {"reproduction_steps": []},
        {"reproduction_steps": [" "]},
        {"suspected_paths": []},
        {"suspected_paths": ["/etc/passwd"]},
        {"suspected_paths": ["../outside.py"]},
        {"suspected_paths": ["reviews\\scores.py"]},
        {"suspected_paths": ["reviews/scores.py", "reviews/scores.py"]},
        {"culprit": {"file": "a.py", "line_start": 5, "line_end": 4, "reason": "x"}},
        {"culprit": {"file": "a.py", "line_start": 0, "line_end": 1, "reason": "x"}},
        {"culprit": {"file": "/a.py", "line_start": 1, "line_end": 1, "reason": "x"}},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"reproduction_test": {"path": "tests/no_ratings.py", "code": "x"}},
        {"reproduction_test": {"path": "../test_x.py", "code": "x"}},
        {"reproduction_test": {"path": "tests/test_x.py", "code": ""}},
        {"stack_trace": "copied"},
    ],
)
def test_a_malformed_output_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 500


def test_prompt_names_every_tool_and_field(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    for field in (*spec.output_class.model_fields, *spec.input_class.model_fields):
        assert f"`{field}`" in text


def test_prompt_states_the_reproduction_rule(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "Do not wrap the call in `pytest.raises`" in text
    assert "raised from the same file as in the event" in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_four_cases_with_unique_ids_and_fixtures(cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(ids) >= 4
    assert len(set(ids)) == len(ids)
    assert len({case.fixture for case in cases}) >= 4


def test_cases_complete_into_inputs_and_use_the_incident_grader(
    spec: LLMSpec, cases: list[Case]
) -> None:
    fixtures = discover()
    for case in cases:
        assert case.fixture in fixtures
        spec.input_class.model_validate({"repo_path": "/repo", **case.payload})
        assert case.grader == "incident"
        assert case.expected.incident is not None
        assert case.payload["ref"] == "head"
        assert case.payload["event"]["release"] == "head"


def test_a_case_has_its_fault_above_the_raising_frame(cases: list[Case]) -> None:
    expected = [case.expected.incident for case in cases if case.expected.incident]
    assert any(item.suspected_path != item.crash_file for item in expected)


def test_each_case_matches_its_planted_defect(cases: list[Case], tmp_path: Path) -> None:
    fixtures = build([case.fixture for case in cases], dest=tmp_path)
    by_name = {fixture.path.name: fixture.path for fixture in fixtures}
    for case in cases:
        expected = case.expected.incident
        assert expected is not None
        root = by_name[case.fixture]
        assert (root / expected.suspected_path).is_file(), case.id
        assert (root / expected.crash_file).is_file(), case.id
        manifest = AGENTS_DIR.parent / "fixtures" / "src" / case.fixture / "defects.yaml"
        defects = yaml.safe_load(manifest.read_text(encoding="utf-8"))["defects"]
        assert expected.suspected_path in {defect["file"] for defect in defects}, case.id
        (raised,) = case.payload["event"]["exceptions"][-1:]
        assert raised["type"] == expected.exception, case.id
        assert raised["frames"][-1]["file"].endswith(expected.crash_file), case.id
