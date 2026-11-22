"""Offline tests of the extraction-modularization agent: manifest, models, prompt, eval cases.

`truth_blueprint` builds the blueprint that says everything the fixture's ground truth says about a
feature. Every case must pass it, and each kind of bad blueprint made from it must fail, before the
judge is asked, naming what it got wrong.
"""

import ast
import copy
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, ExpectedGroundTruth, load_cases
from evals.graders import Grade, grade
from evals.graders.ground_truth import check_ground_truth, load_truth
from evals.graders.llm_judge import JudgeVerdict
from fixtures.build import build, discover
from fixtures.ground_truth import Feature, GroundTruth
from registry.decomposition_schemas import CodeArchaeologyOutput, ExtractionBlueprint
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from tests.evals.test_ground_truth_grader import truth_blueprint
from tools import TOOLS, tool_defs

AGENT_DIR = AGENTS_DIR / "extraction-modularization"
FIXTURE = "legacy-billing"
TOOL_NAMES = {"git.read_file", "rg.search", "deps.graph", "deps.impact", "deps.call_graph"}
CORE = "app/core.py::run"
HELPER = "app/core.py::helper"
MAIN = "app/cli.py::main"
STORE = "app/store.py::save"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("extraction-modularization", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


@pytest.fixture(scope="module")
def truth() -> GroundTruth:
    return load_truth(FIXTURE)


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    assert FIXTURE in discover()
    (fixture,) = build([FIXTURE], dest=tmp_path_factory.mktemp("repos"))
    return fixture.path


def _archaeology() -> dict[str, Any]:
    return {
        "summary": "A program.",
        "modules": [{"path": "app/cli.py", "role": "The command line.", "imports": []}],
        "data_flows": [],
        "dead_code": [],
        "dynamic_lookups": [],
        "execution_paths": [],
    }


def _input(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "repo_path": "/repo",
        "archaeology": _archaeology(),
        "feature": {"name": "The feature", "start": CORE},
    }
    return value | overrides


def _feature(**overrides: Any) -> dict[str, Any]:
    return {"feature": {"name": "The feature", "start": CORE} | overrides}


def _use(**overrides: Any) -> dict[str, Any]:
    return {"symbol": STORE, "used_by": CORE, "need": "Saves the rows."} | overrides


def _call(**overrides: Any) -> dict[str, Any]:
    return {"caller": MAIN, "callee": CORE, "expects": "The count."} | overrides


def _state(**overrides: Any) -> dict[str, Any]:
    state = {"symbol": CORE, "state": "The counters.", "access": "writes", "cut": "Return it."}
    return state | overrides


def _test(**overrides: Any) -> dict[str, Any]:
    return {"path": "tests/test_core.py", "pins": "The amounts."} | overrides


def _output(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "feature": "The feature",
        "start": CORE,
        "summary": "It does one thing.",
        "inputs": ["The day."],
        "outputs": ["A count."],
        "invariants": ["Nothing happens twice."],
        "symbols": [{"symbol": CORE, "role": "Starts it."}, {"symbol": HELPER, "role": "Helps."}],
        "uses": [_use()],
        "called_from": [_call()],
        "state": [_state()],
        "pinning_tests": [_test()],
        "open_questions": [],
    }
    return value | overrides


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "extraction-modularization@0.1.0"
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
    assert spec.input_class.__name__ == "ExtractionModularizationInput"
    # What code-archaeology returns goes in unchanged.
    assert spec.input_class.model_fields["archaeology"].annotation is CodeArchaeologyOutput
    # The shared model, so that a pipeline can turn it into a ticket.
    assert spec.output_class is ExtractionBlueprint


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


@pytest.mark.parametrize("start", [CORE, "app/core.py", "app/core.py::Rater.rate"])
def test_input_takes_a_repository_an_archaeology_and_a_feature(spec: LLMSpec, start: str) -> None:
    value = spec.input_class.model_validate(_input(**_feature(start=start)))
    assert spec.input_class.model_validate_json(value.model_dump_json()) == value


def test_the_name_of_the_feature_is_stripped(spec: LLMSpec) -> None:
    value = spec.input_class.model_validate(_input(**_feature(name="  Late fees ")))
    assert value.model_dump()["feature"]["name"] == "Late fees"


@pytest.mark.parametrize("field", ["repo_path", "archaeology", "feature"])
def test_every_input_field_is_required(spec: LLMSpec, field: str) -> None:
    value = _input()
    del value[field]
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(value)


@pytest.mark.parametrize(
    "bad",
    [
        _feature(name=""),
        _feature(name="   "),
        _feature(name="x" * 121),
        _feature(start=""),
        _feature(start="run"),
        _feature(start="/etc/passwd.py::main"),
        _feature(start="../outside.py::main"),
        _feature(start="app/../../outside.py::main"),
        _feature(start="app/core.py::"),
        _feature(start="app/core.py::two words"),
        _feature(start=f"{'a' * 160}.py::main"),
        _feature(symbols=[]),
        {"feature": "The feature"},
        {"archaeology": {"summary": "A program."}},
        {"archaeology": _archaeology() | {"features": []}},
        {"ground_truth": "x"},
    ],
)
def test_a_malformed_input_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.input_class.model_validate(_input(**bad))


# --- output


def test_output_round_trips_through_json(spec: LLMSpec, truth: GroundTruth) -> None:
    for value in (_output(), *(truth_blueprint(feature) for feature in truth.features)):
        parsed = spec.output_class.model_validate(value)
        assert spec.output_class.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    "good",
    [
        {"uses": [], "called_from": [], "state": [], "pinning_tests": []},
        {"inputs": [], "outputs": []},
        # A class or a module in `symbols` covers what is in it.
        {"symbols": [{"symbol": "app/core.py", "role": "All of it."}]},
        {"start": "app/core.py::Rater.rate", "symbols": [
            {"symbol": "app/core.py::Rater", "role": "The class."},
            {"symbol": CORE, "role": "Starts it."},
        ]},
    ],
)  # fmt: skip
def test_an_output_with_fewer_ties_is_valid(spec: LLMSpec, good: dict[str, Any]) -> None:
    spec.output_class.model_validate(_output(**good))


@pytest.mark.parametrize(
    "bad",
    [
        # Every list has a cap, and a blueprint without rules or code is none.
        {"inputs": ["x"] * 11},
        {"outputs": ["x"] * 11},
        {"invariants": ["x"] * 13},
        {"invariants": []},
        {"symbols": [{"symbol": CORE, "role": "x"}] * 25},
        {"symbols": []},
        {"uses": [_use()] * 21},
        {"called_from": [_call()] * 11},
        {"state": [_state()] * 11},
        {"pinning_tests": [_test()] * 9},
        {"open_questions": ["x"] * 7},
        # So does every string.
        {"feature": ""},
        {"feature": "x" * 121},
        {"summary": ""},
        {"summary": "x" * 801},
        {"inputs": ["x" * 301]},
        {"invariants": ["  "]},
        {"symbols": [{"symbol": CORE, "role": "x" * 121}]},
        {"uses": [_use(need="x" * 121)]},
        {"uses": [_use(symbol=f"a.py::{'f' * 160}")]},
        {"called_from": [_call(expects="")]},
        {"state": [_state(cut="x" * 121)]},
        {"pinning_tests": [_test(path=f"{'a' * 120}.py")]},
        # Symbols and paths are written like the tools write them.
        {"start": "app.core.run"},
        {"symbols": [{"symbol": "run", "role": "x"}]},
        {"uses": [_use(symbol="app.store.save")]},
        {"pinning_tests": [_test(path="/tests/test_core.py")]},
        {"pinning_tests": [_test(path="../tests/test_core.py")]},
        {"pinning_tests": [_test(path="two words.py")]},
        {"state": [_state(access="touches")]},
        # The boundary holds together.
        {"start": "app/core.py::other"},
        {"uses": [_use(symbol=HELPER)]},
        {"called_from": [_call(caller=HELPER)]},
        {"uses": [_use(used_by=MAIN)]},
        {"called_from": [_call(callee=STORE)]},
        {"state": [_state(symbol=MAIN)]},
        {"ticket": {}},
    ],
)
def test_a_malformed_output_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(**bad))


def test_the_longest_blueprint_fits_the_output_limit(spec: LLMSpec) -> None:
    prose, line = "x" * 300, "x" * 120

    def symbol(number: int) -> str:
        return f"{'a/' * 60}b.py::{'c' * 31}{number:03d}"

    inside = [symbol(number) for number in range(24)]
    assert len(inside[0]) == 160
    longest = _output(
        feature=line,
        start=inside[0],
        summary="x" * 800,
        inputs=[prose] * 10,
        outputs=[prose] * 10,
        invariants=[prose] * 12,
        symbols=[{"symbol": item, "role": line} for item in inside],
        uses=[_use(symbol=symbol(100 + n), used_by=inside[0], need=line) for n in range(20)],
        called_from=[
            _call(caller=symbol(200 + n), callee=inside[0], expects=line) for n in range(10)
        ],
        state=[
            _state(symbol=item, state=line, access="reads_and_writes", cut=line)
            for item in inside[:10]
        ],
        pinning_tests=[_test(path="t" * 120, pins=line)] * 8,
        open_questions=[prose] * 6,
    )
    parsed = spec.output_class.model_validate(longest)
    assert len(parsed.model_dump_json()) / 3 < spec.limits.max_output_tokens


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
    for field in CodeArchaeologyOutput.model_fields:
        assert f"`{field}`" in text


def test_prompt_asks_for_plain_words(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "another language" in text
    assert "no code blocks" in text
    # It shows no code block itself.
    assert "```" not in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def _feature_of(case: Case, truth: GroundTruth) -> Feature:
    assert case.expected.ground_truth is not None
    (wanted,) = case.expected.ground_truth.features
    (feature,) = [entry for entry in truth.features if entry.id == wanted]
    return feature


def _strings(value: object) -> Iterable[str]:
    """Return every string in `value`, keys aside."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _named_paths(archaeology: CodeArchaeologyOutput) -> tuple[set[str], set[str]]:
    """Return the module paths and the symbols that `archaeology` names."""
    paths = {entry.path for entry in archaeology.modules}
    paths |= {path for entry in archaeology.modules for path in entry.imports}
    symbols = {step.symbol for flow in archaeology.data_flows for step in flow.steps}
    symbols |= {entry.symbol for entry in archaeology.dead_code}
    for lookup in archaeology.dynamic_lookups:
        symbols |= {lookup.symbol, lookup.lookup_site}
    for path in archaeology.execution_paths:
        for call in path.calls:
            symbols |= {call.caller, *([call.callee] if call.callee else [])}
    return paths, symbols


def _defined(path: Path) -> set[str]:
    """Return the qualified names that the module at `path` defines."""
    names: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.ClassDef):
                names.add(f"{prefix}{node.name}")
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.Assign):
                names.update(
                    f"{prefix}{target.id}"
                    for target in node.targets
                    if isinstance(target, ast.Name)
                )

    visit(ast.parse(path.read_text(encoding="utf-8")).body, "")
    return names


def test_there_are_at_least_four_cases_with_unique_ids_and_payloads(cases: list[Case]) -> None:
    assert len(cases) >= 4
    assert len({case.id for case in cases}) == len(cases)
    assert len({json.dumps(case.payload, sort_keys=True) for case in cases}) == len(cases)
    assert len({case.payload["feature"]["name"] for case in cases}) == len(cases)


def test_cases_complete_into_inputs_and_use_the_rubric_grader(
    spec: LLMSpec, cases: list[Case]
) -> None:
    for case in cases:
        assert case.fixture == FIXTURE
        assert case.grader == "rubric"
        assert case.expected.rubric is not None
        assert case.expected.ground_truth is not None
        assert set(case.payload) == {"archaeology", "feature"}
        spec.input_class.model_validate({"repo_path": "/repo", **case.payload})


def test_the_cases_cover_every_feature_from_different_archaeology(
    cases: list[Case], truth: GroundTruth
) -> None:
    seen: dict[str, set[str]] = {}
    for case in cases:
        feature = _feature_of(case, truth)
        assert case.payload["feature"]["start"] == feature.symbols[0]
        text = json.dumps(case.payload["archaeology"], sort_keys=True)
        seen.setdefault(feature.id, set()).add(text)
    assert set(seen) == {feature.id for feature in truth.features}
    assert all(len(texts) >= 2 for texts in seen.values())


def test_every_rubric_forbids_code_and_weighs_prose(cases: list[Case]) -> None:
    for case in cases:
        rubric = case.expected.rubric
        assert rubric is not None
        assert {"```", "def ", "=="} <= set(rubric.forbidden), case.id
        assert rubric.paths_exist == ("pinning_tests.*.path",)
        required = {item.id for item in rubric.criteria if item.required}
        assert {
            "invariants-real-and-testable",
            "enough-for-a-coder",
            "language-agnostic",
        } <= required, case.id


def test_the_archaeology_names_only_what_the_built_repository_has(
    cases: list[Case], repo: Path
) -> None:
    names = {path.name for path in repo.rglob("*") if ".git" not in path.parts}
    assert not names & {"ground-truth.yaml", "defects.yaml", "spec.yaml"}
    for case in cases:
        archaeology = CodeArchaeologyOutput.model_validate(case.payload["archaeology"])
        paths, symbols = _named_paths(archaeology)
        symbols.add(case.payload["feature"]["start"])
        for symbol in symbols:
            path, _, name = symbol.partition("::")
            paths.add(path)
            if name:
                assert name in _defined(repo / path), f"{case.id}: {symbol}"
        assert [path for path in sorted(paths) if not (repo / path).is_file()] == [], case.id


def test_the_archaeology_agrees_with_the_ground_truth(
    cases: list[Case], truth: GroundTruth
) -> None:
    """No payload misleads: all dead code, no decoy among it, and no dynamic call guessed."""
    dead = ExpectedGroundTruth(dead_code=tuple(entry.id for entry in truth.dead_code))
    for case in cases:
        found = check_ground_truth(dead, case.payload["archaeology"], truth)
        assert found == (), case.id


def _feature_text(feature: Feature) -> list[str]:
    """Return what the ground truth says about `feature` and the agent must find out itself."""
    sites = (*feature.called_from, *feature.state)
    constants = [symbol for symbol in feature.symbols if symbol.rpartition("::")[2].isupper()]
    return [
        feature.id,
        feature.title,
        *(site.snippet for site in sites),
        *feature.tests,
        *constants,
    ]


def test_the_prompt_and_the_payloads_carry_no_feature_of_the_ground_truth(
    spec: LLMSpec, cases: list[Case], truth: GroundTruth
) -> None:
    prompt = spec.system_prompt_file.read_text(encoding="utf-8")
    assert "legacy_billing" not in prompt
    assert "legacy-billing" not in prompt
    payloads = [text for case in cases for text in _strings(dict(case.payload))]
    for feature in truth.features:
        for text in _feature_text(feature):
            assert text not in prompt
            assert [found for found in payloads if text in found] == []
        for symbol in (*feature.symbols, *feature.uses):
            assert symbol not in prompt


def test_a_payload_names_only_the_name_and_start_of_its_feature(
    cases: list[Case], truth: GroundTruth
) -> None:
    for case in cases:
        feature = _feature_of(case, truth)
        assert set(case.payload["feature"]) == {"name", "start"}
        # The archaeology never lists the boundary: part of every feature is missing from it, and
        # no string of it ties the feature's symbols together.
        strings = set(_strings(case.payload["archaeology"]))
        missing = [symbol for symbol in feature.symbols if symbol not in strings]
        assert len(missing) >= len(feature.symbols) / 2, case.id
        for text in strings:
            names = [s for s in feature.symbols if s.rpartition("::")[2] in text]
            assert len(names) <= 2, f"{case.id}: {text}"


# --- grading


def _no_judge(*_: object) -> JudgeVerdict:
    raise AssertionError("the judge must not be called")


class _Judge:
    """A rubric judge that passes every criterion, so only the deterministic checks decide."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_: Any, **__: Any) -> JudgeVerdict:
        self.calls += 1
        return JudgeVerdict(score=1.0, passed=True, reason="fine")


def _grade(case: Case, blueprint: dict[str, Any], repo: Path, judge: Any) -> Grade:
    return grade(case, blueprint, _no_judge, repo_path=repo, rubric_judge=judge)


def _assert_fails(case: Case, blueprint: dict[str, Any], repo: Path, *reasons: str) -> None:
    """The blueprint fails before the judge is asked, and the reason names what is wrong."""
    found = _grade(case, blueprint, repo, _no_judge)
    assert not found.passed, case.id
    for reason in reasons:
        assert reason in found.reason, case.id


def test_the_blueprint_built_from_the_ground_truth_passes_every_case(
    cases: list[Case], truth: GroundTruth, repo: Path
) -> None:
    judge = _Judge()
    for case in cases:
        found = _grade(case, truth_blueprint(_feature_of(case, truth)), repo, judge)
        assert found.passed, f"{case.id}: {found.reason}"
    assert judge.calls == len(cases)


def test_the_blueprint_of_another_feature_fails(
    cases: list[Case], truth: GroundTruth, repo: Path
) -> None:
    for case in cases:
        feature = _feature_of(case, truth)
        for other in truth.features:
            if other.id != feature.id:
                _assert_fails(case, truth_blueprint(other), repo, f"feature {feature.id}")


@pytest.mark.parametrize(
    ("field", "key", "where"),
    [
        ("symbols", "symbol", "symbols"),
        ("called_from", "caller", "the callers of called_from"),
        ("state", "symbol", "state"),
        ("pinning_tests", "path", "pinning_tests"),
    ],
)
def test_each_missing_entry_fails_the_cases_of_its_feature(
    cases: list[Case], truth: GroundTruth, repo: Path, field: str, key: str, where: str
) -> None:
    for case in cases:
        feature = _feature_of(case, truth)
        whole = truth_blueprint(feature)
        assert whole[field]
        for entry in whole[field]:
            answer = copy.deepcopy(whole)
            answer[field] = [item for item in whole[field] if item[key] != entry[key]]
            _assert_fails(
                case, answer, repo, f"feature {feature.id}", f"{entry[key]} is missing from {where}"
            )


def test_one_missing_use_passes_and_two_fail(
    cases: list[Case], truth: GroundTruth, repo: Path
) -> None:
    for case in cases:
        feature = _feature_of(case, truth)
        answer = truth_blueprint(feature)
        first, second, *rest = answer["uses"]
        assert _grade(case, answer | {"uses": [second, *rest]}, repo, _Judge()).passed
        _assert_fails(
            case,
            answer | {"uses": rest},
            repo,
            f"{first['symbol']} is missing from uses",
            f"{second['symbol']} is missing from uses",
        )


def test_code_outside_the_feature_listed_as_part_of_it_fails(
    cases: list[Case], truth: GroundTruth, repo: Path
) -> None:
    for case in cases:
        feature = _feature_of(case, truth)
        outside = [*feature.uses, *(site.symbol for site in feature.called_from)]
        for symbol in outside:
            answer = truth_blueprint(feature)
            answer["symbols"].append({"symbol": symbol, "role": "Part of it."})
            _assert_fails(
                case,
                answer,
                repo,
                f"feature {feature.id}",
                f"{symbol} is listed in symbols, but {symbol} is outside the feature",
            )


@pytest.mark.parametrize(
    ("field", "text", "reason"),
    [
        ("inputs", "```\nday\n```", "forbidden string '```' in inputs"),
        ("outputs", "def apply(day)", "forbidden string 'def ' in outputs"),
        ("invariants", "the fee == 500", "forbidden string '==' in invariants"),
        ("invariants", "len(invoices) stays the same", "forbidden string 'len(' in invariants"),
    ],
)
def test_code_in_the_prose_fails_every_case(
    cases: list[Case], truth: GroundTruth, repo: Path, field: str, text: str, reason: str
) -> None:
    for case in cases:
        answer = truth_blueprint(_feature_of(case, truth))
        answer[field] = [*answer[field], text]
        ExtractionBlueprint.model_validate(answer)
        _assert_fails(case, answer, repo, reason)


def test_a_pinning_test_that_does_not_exist_fails_every_case(
    cases: list[Case], truth: GroundTruth, repo: Path
) -> None:
    for case in cases:
        answer = truth_blueprint(_feature_of(case, truth))
        answer["pinning_tests"].append({"path": "tests/test_invented.py", "pins": "Nothing."})
        _assert_fails(case, answer, repo, "tests/test_invented.py")


@pytest.mark.parametrize("field", ["inputs", "outputs"])
def test_an_empty_field_fails_every_case(
    cases: list[Case], truth: GroundTruth, repo: Path, field: str
) -> None:
    for case in cases:
        answer = truth_blueprint(_feature_of(case, truth)) | {field: []}
        ExtractionBlueprint.model_validate(answer)
        _assert_fails(case, answer, repo, f"required field {field} is empty")
