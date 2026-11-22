"""Offline tests of the code-archaeology agent: manifest, models, prompt, eval cases.

`truth_answer` builds the answer that says everything the fixture's ground truth says. Every case
must pass it, and each kind of bad answer made from it must fail naming the entry it got wrong.
"""

import ast
import copy
import json
from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from evals.graders import Grade, grade
from evals.graders.ground_truth import load_truth
from evals.graders.llm_judge import JudgeVerdict
from fixtures.build import build, discover
from fixtures.ground_truth import GroundTruth
from registry.decomposition_schemas import CodeArchaeologyOutput
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tests.evals.test_ground_truth_grader import truth_answer
from tools import TOOLS, tool_defs

AGENT_DIR = AGENTS_DIR / "code-archaeology"
FIXTURE = "legacy-billing"
TOOL_NAMES = {
    "git.read_file",
    "rg.search",
    "deps.graph",
    "deps.impact",
    "deps.dead_code",
    "deps.call_graph",
}
MAIN = "legacy_billing/cli.py::main"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("code-archaeology", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


@pytest.fixture(scope="module")
def truth() -> GroundTruth:
    return load_truth(FIXTURE)


@pytest.fixture
def answer(truth: GroundTruth) -> dict[str, Any]:
    return copy.deepcopy(truth_answer(truth))


def _resolved(**overrides: Any) -> dict[str, Any]:
    call: dict[str, Any] = {
        "status": "resolved",
        "caller": MAIN,
        "callee": "legacy_billing/billing.py::run_billing",
        "line": 40,
        "expression": None,
        "reason": None,
    }
    return call | overrides


def _unresolved(**overrides: Any) -> dict[str, Any]:
    call = _resolved(status="unresolved", callee=None, expression="hook", reason="container")
    return call | overrides


def _dead(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "symbol": "legacy_billing/a.py::f",
        "kind": "function",
        "line": 3,
        "rule": "vulture",
        "confidence": 60,
        "evidence": "deps.dead_code lists it and no other file names it.",
    }
    return entry | overrides


def _lookup(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "symbol": "legacy_billing/a.py::export_csv",
        "kind": "function",
        "line": 3,
        "rule": "vulture",
        "confidence": 60,
        "lookup_site": "legacy_billing/a.py::export",
        "lookup": "getattr with a computed name",
    }
    return entry | overrides


def _step(symbol: str = "legacy_billing/a.py::f") -> dict[str, Any]:
    return {"symbol": symbol, "does": "reads it"}


def _output(**overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "summary": "The command line starts one billing run.",
        "modules": [{"path": "legacy_billing/cli.py", "role": "Command line.", "imports": []}],
        "data_flows": [{"data": "A usage row.", "steps": [_step(), _step(MAIN)]}],
        "dead_code": [_dead()],
        "dynamic_lookups": [_lookup()],
        "execution_paths": [{"title": "The run.", "calls": [_resolved(), _unresolved()]}],
    }
    return output | overrides


def _path(*calls: dict[str, Any], title: str = "The run.") -> dict[str, Any]:
    return {"execution_paths": [{"title": title, "calls": list(calls)}]}


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "code-archaeology@0.1.0"
    assert spec.team == "decomp"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "high"
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 25
    assert spec.limits.timeout_s == 600
    assert spec.limits.max_output_tokens == 16000
    assert spec.limits.max_tokens_total == 600000


def test_the_agent_is_read_only(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) == TOOL_NAMES
    assert set(spec.tools.allow) <= set(TOOLS)
    assert len(tool_defs(spec.tools.allow, spec.tools.deny)) == len(spec.tools.allow)
    # No sandbox, no files policy and no tool that writes: the agent only reads the repository.
    assert spec.sandbox is False
    assert spec.sandbox_profile is None
    assert spec.files is None


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "CodeArchaeologyInput"
    # The shared model, so that the agent that plans an extraction can take it as input.
    assert spec.output_class is CodeArchaeologyOutput


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


@pytest.mark.parametrize(
    "entry_point", [MAIN, "legacy_billing/cli.py", "legacy_billing/rating.py::Rater.rate"]
)
def test_input_takes_a_repository_and_a_symbol(spec: LLMSpec, entry_point: str) -> None:
    value = spec.input_class.model_validate({"repo_path": "/repo", "entry_point": entry_point})
    assert spec.input_class.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "bad",
    [
        {"repo_path": "/repo"},
        {"entry_point": MAIN},
        {"repo_path": "/repo", "entry_point": ""},
        {"repo_path": "/repo", "entry_point": "main"},
        {"repo_path": "/repo", "entry_point": "/etc/passwd.py::main"},
        {"repo_path": "/repo", "entry_point": "../outside.py::main"},
        {"repo_path": "/repo", "entry_point": "a/../../outside.py::main"},
        {"repo_path": "/repo", "entry_point": "legacy_billing/cli.py::"},
        {"repo_path": "/repo", "entry_point": "legacy_billing/cli.py::two words"},
        {"repo_path": "/repo", "entry_point": f"{'a' * 160}.py::main"},
        {"repo_path": "/repo", "entry_point": MAIN, "ground_truth": "x"},
    ],
)
def test_a_malformed_input_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(bad)


# --- output


def test_output_round_trips_through_json(spec: LLMSpec, answer: dict[str, Any]) -> None:
    for value in (_output(), answer):
        output = spec.output_class.model_validate(value)
        assert spec.output_class.model_validate_json(output.model_dump_json()) == output


@pytest.mark.parametrize(
    "bad",
    [
        # Every list has a cap.
        {"modules": _output()["modules"] * 41},
        {"data_flows": _output()["data_flows"] * 11},
        {"dead_code": [_dead()] * 31},
        {"dynamic_lookups": [_lookup()] * 21},
        {"execution_paths": _output()["execution_paths"] * 15},
        {"modules": [{"path": "a.py", "role": "x", "imports": ["b.py"] * 21}]},
        {"data_flows": [{"data": "x", "steps": [_step()] * 13}]},
        {"data_flows": [{"data": "x", "steps": [_step()]}]},
        _path(*[_resolved()] * 15),
        _path(),
        # So does every string.
        {"summary": "x" * 1501},
        {"summary": ""},
        {"modules": [{"path": "a.py", "role": "x" * 161, "imports": []}]},
        {"modules": [{"path": f"{'a' * 120}.py", "role": "x", "imports": []}]},
        {"modules": [{"path": "/a.py", "role": "x", "imports": []}]},
        {"data_flows": [{"data": "x" * 161, "steps": [_step(), _step()]}]},
        {"dead_code": [_dead(symbol=f"a.py::{'f' * 160}")]},
        {"dead_code": [_dead(evidence="x" * 241)]},
        {"dynamic_lookups": [_lookup(lookup="x" * 161)]},
        _path(_resolved(), title="x" * 121),
        _path(_unresolved(expression="x" * 201)),
        # Dead code needs the tool's evidence, in the tool's words.
        {"dead_code": [_dead(evidence="")]},
        {"dead_code": [{key: value for key, value in _dead().items() if key != "evidence"}]},
        {"dead_code": [_dead(rule="guess")]},
        {"dead_code": [_dead(kind="variable")]},
        {"dead_code": [_dead(line=0)]},
        {"dead_code": [_dead(confidence=101)]},
        # A resolved call is an edge, and an unresolved one names its site and no target.
        _path(_resolved(callee=None)),
        _path(_resolved(reason="container")),
        _path(_resolved(expression="hook")),
        _path(_unresolved(callee="legacy_billing/a.py::f")),
        _path(_unresolved(expression=None)),
        _path(_unresolved(expression="")),
        _path(_unresolved(reason=None)),
        _path(_unresolved(reason="no idea")),
        _path(_unresolved(line=0)),
        _path(_resolved(status="guessed")),
        {"blueprint": []},
    ],
)
def test_a_malformed_output_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


def test_the_whole_ground_truth_fits_the_output_limit(
    spec: LLMSpec, answer: dict[str, Any]
) -> None:
    assert len(json.dumps(answer)) / 3 < spec.limits.max_output_tokens


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


def test_prompt_names_every_reason_a_call_is_unresolved(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    schema = anthropic.transform_schema(spec.output_class)
    reasons = [
        option
        for part in schema["$defs"]["PathCall"]["properties"]["reason"]["anyOf"]
        for option in part.get("enum", [])
    ]
    assert len(reasons) == 12
    assert [reason for reason in reasons if reason not in text] == []


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


def _copied_text(truth: GroundTruth) -> list[str]:
    """Return the prose and code of the ground truth, which must not be told to the agent."""
    return [
        *(flow.data for flow in truth.data_flows),
        *(step.does for flow in truth.data_flows for step in flow.steps),
        *(chain.title for chain in truth.call_chains),
        *(entry.symbol for entry in truth.dead_code),
        *(entry.symbol for entry in truth.decoys),
        *(entry.snippet for entry in truth.dynamic_calls),
    ]


def test_the_prompt_and_the_cases_copy_no_ground_truth(
    spec: LLMSpec, cases: list[Case], truth: GroundTruth
) -> None:
    prompt = spec.system_prompt_file.read_text(encoding="utf-8")
    payloads = json.dumps([dict(case.payload) for case in cases])
    for text in _copied_text(truth):
        assert text not in prompt
        assert text not in payloads
    assert "legacy_billing" not in prompt
    assert "legacy-billing" not in prompt


# --- eval cases


def test_there_are_at_least_four_cases_with_unique_ids_and_entry_points(
    cases: list[Case],
) -> None:
    assert len(cases) >= 4
    assert len({case.id for case in cases}) == len(cases)
    assert len({case.payload["entry_point"] for case in cases}) == len(cases)


def test_cases_complete_into_inputs_and_use_the_ground_truth_grader(
    spec: LLMSpec, cases: list[Case]
) -> None:
    for case in cases:
        assert case.fixture == FIXTURE
        assert case.grader == "ground_truth"
        assert case.expected.ground_truth is not None
        assert set(case.payload) == {"entry_point"}
        spec.input_class.model_validate({"repo_path": "/repo", **case.payload})


def test_the_cases_cover_the_ground_truth_between_them(
    cases: list[Case], truth: GroundTruth
) -> None:
    blocks = [case.expected.ground_truth for case in cases if case.expected.ground_truth]
    dead = {entry for block in blocks for entry in block.dead_code}
    flows = {entry for block in blocks for entry in block.data_flows}
    chains = {entry for block in blocks for entry in block.call_chains}
    dynamic = {entry for block in blocks for entry in block.dynamic_calls}
    assert dead == {entry.id for entry in truth.dead_code}
    assert len(flows) >= 2
    assert chains == {entry.id for entry in truth.call_chains}
    assert dynamic == {entry.id for entry in truth.dynamic_calls}


def test_the_built_repository_has_the_entry_points_and_no_ground_truth(
    cases: list[Case], tmp_path: Path
) -> None:
    assert FIXTURE in discover()
    (fixture,) = build([FIXTURE], dest=tmp_path)
    names = {path.name for path in fixture.path.rglob("*") if ".git" not in path.parts}
    assert not names & {"ground-truth.yaml", "defects.yaml", "spec.yaml"}
    for case in cases:
        path, _, symbol = case.payload["entry_point"].partition("::")
        tree = ast.parse((fixture.path / path).read_text(encoding="utf-8"))
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert symbol in defined, case.id


# --- grading


def _no_judge(*_: object) -> JudgeVerdict:
    raise AssertionError("the judge must not be called")


def _failing(cases: list[Case], answer: dict[str, Any]) -> dict[str, Grade]:
    """Return the failed grade of each case that `answer` fails, by case id."""
    CodeArchaeologyOutput.model_validate(answer)
    grades = {case.id: grade(case, answer, _no_judge) for case in cases}
    return {case_id: found for case_id, found in grades.items() if not found.passed}


def test_the_answer_built_from_the_ground_truth_passes_every_case(
    cases: list[Case], answer: dict[str, Any]
) -> None:
    assert _failing(cases, answer) == {}


def test_a_decoy_listed_as_dead_fails_every_case(
    cases: list[Case], truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for decoy in truth.decoys:
        bad = copy.deepcopy(answer)
        bad["dead_code"].append(_dead(symbol=decoy.symbol))
        failed = _failing(cases, bad)
        assert set(failed) == {case.id for case in cases}, decoy.id
        assert all(decoy.id in found.reason for found in failed.values()), decoy.id


def _users(cases: list[Case], field: str, entry_id: str) -> set[str]:
    return {
        case.id
        for case in cases
        if case.expected.ground_truth and entry_id in getattr(case.expected.ground_truth, field)
    }


def _assert_named(cases: list[Case], answer: dict[str, Any], field: str, entry_id: str) -> None:
    """Assert that the cases naming `entry_id` fail with it in the reason, and no others fail."""
    failed = _failing(cases, answer)
    assert set(failed) == _users(cases, field, entry_id), entry_id
    assert all(entry_id in found.reason for found in failed.values()), entry_id


def test_a_planted_dead_symbol_that_is_missing_fails_the_cases_that_name_it(
    cases: list[Case], truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for entry in truth.dead_code:
        bad = copy.deepcopy(answer)
        bad["dead_code"] = [item for item in bad["dead_code"] if item["symbol"] != entry.symbol]
        assert _users(cases, "dead_code", entry.id)
        _assert_named(cases, bad, "dead_code", entry.id)


def test_a_missing_data_flow_fails_the_cases_that_name_it(
    cases: list[Case], truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for index, flow in enumerate(truth.data_flows):
        bad = copy.deepcopy(answer)
        del bad["data_flows"][index]
        _assert_named(cases, bad, "data_flows", flow.id)


def test_a_missing_call_of_a_chain_fails_the_cases_that_name_it(
    cases: list[Case], truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for chain in truth.call_chains:
        missing = chain.calls[-1]
        bad = copy.deepcopy(answer)
        for path in bad["execution_paths"]:
            path["calls"] = [
                call
                for call in path["calls"]
                if (call["caller"], call["callee"]) != (missing.caller, missing.callee)
            ]
        assert _users(cases, "call_chains", chain.id)
        _assert_named(cases, bad, "call_chains", chain.id)


def test_a_dynamic_call_that_is_not_reported_fails_the_cases_that_name_it(
    cases: list[Case], truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for entry in truth.dynamic_calls:
        bad = copy.deepcopy(answer)
        bad["execution_paths"] = [
            path for path in bad["execution_paths"] if path["title"] != entry.id
        ]
        assert _users(cases, "dynamic_calls", entry.id)
        _assert_named(cases, bad, "dynamic_calls", entry.id)


def test_a_dynamic_call_reported_as_a_plain_call_fails_every_case(
    cases: list[Case], truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for entry in truth.dynamic_calls:
        bad = copy.deepcopy(answer)
        bad["execution_paths"][0]["calls"].append(
            _resolved(caller=entry.symbol, callee=entry.targets[0])
        )
        failed = _failing(cases, bad)
        assert set(failed) == {case.id for case in cases}, entry.id
        assert all(entry.id in found.reason for found in failed.values()), entry.id
