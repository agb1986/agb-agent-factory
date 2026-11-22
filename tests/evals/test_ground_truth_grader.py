"""Tests for the ground_truth grader: the case schema, the matching rules and the merge into `grade`.

The rules are tested on the ground truth of `legacy-billing`, with an answer built from it and then
changed one entry at a time. `truth_answer` is also what `tests/agents/test_code_archaeology.py`
grades the agent's cases with, and `truth_blueprint` does the same for
`tests/agents/test_extraction_modularization.py`. Nothing here calls a model.
"""

import copy
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import Case, ExpectedGroundTruth
from evals.graders import grade
from evals.graders.ground_truth import check_ground_truth, load_truth
from evals.graders.llm_judge import JudgeVerdict
from factory.exit import UsageError
from fixtures.build import discover
from fixtures.ground_truth import Feature, GroundTruth
from registry.decomposition_schemas import CodeArchaeologyOutput, ExtractionBlueprint

FIXTURE = "legacy-billing"
HOOKS = {"dynamic_calls": ["dynamic-hooks"]}
RUBRIC = {"threshold": 0.7, "criteria": [{"id": "clear", "text": "It is clear."}]}
POST_INVOICE = "legacy_billing/billing.py::post_invoice"


def truth_answer(truth: GroundTruth) -> dict[str, Any]:
    """Return the output that says everything `truth` says, as the agent would have to say it."""

    def resolved(caller: str, callee: str) -> dict[str, Any]:
        return {
            "status": "resolved",
            "caller": caller,
            "callee": callee,
            "line": 1,
            "expression": None,
            "reason": None,
        }

    answer = {
        "summary": "Everything the ground truth says.",
        "modules": [
            {"path": m.path, "role": m.role, "imports": list(m.imports)} for m in truth.modules
        ],
        "data_flows": [
            {
                "data": flow.data,
                "steps": [{"symbol": step.symbol, "does": step.does} for step in flow.steps],
            }
            for flow in truth.data_flows
        ],
        "dead_code": [
            {
                "symbol": entry.symbol,
                "kind": entry.kind,
                "line": 1,
                "rule": "vulture",
                "confidence": 60,
                "evidence": "deps.dead_code lists it and no other file names it.",
            }
            for entry in truth.dead_code
        ],
        "dynamic_lookups": [],
        "execution_paths": [
            *(
                {
                    "title": chain.title,
                    "calls": [resolved(call.caller, call.callee) for call in chain.calls],
                }
                for chain in truth.call_chains
            ),
            *(
                {
                    "title": entry.id,
                    "calls": [
                        {
                            "status": "unresolved",
                            "caller": entry.symbol,
                            "callee": None,
                            "line": 1,
                            "expression": entry.snippet,
                            "reason": "container",
                        }
                    ],
                }
                for entry in truth.dynamic_calls
            ),
        ],
    }
    return CodeArchaeologyOutput.model_validate(answer).model_dump(mode="json")


def truth_blueprint(feature: Feature) -> dict[str, Any]:
    """Return the blueprint that says everything `feature` says, as the agent would have to say it."""
    start = feature.symbols[0]
    blueprint = {
        "feature": feature.title,
        "start": start,
        "summary": "Everything the ground truth says about the feature.",
        "inputs": ["The day the run is for."],
        "outputs": ["How many customers it changed."],
        "invariants": ["Running it twice for the same day changes nothing the second time."],
        "symbols": [{"symbol": symbol, "role": "Part of it."} for symbol in feature.symbols],
        "uses": [
            {"symbol": symbol, "used_by": start, "need": "Its result."} for symbol in feature.uses
        ],
        "called_from": [
            {"caller": site.symbol, "callee": start, "expects": "The count."}
            for site in feature.called_from
        ],
        "state": [
            {
                "symbol": site.symbol,
                "state": "The counters of the run.",
                "access": "reads_and_writes",
                "cut": "Return the count and let the caller add it.",
            }
            for site in feature.state
        ],
        "pinning_tests": [{"path": path, "pins": "Its amounts."} for path in feature.tests],
        "open_questions": [],
    }
    return ExtractionBlueprint.model_validate(blueprint).model_dump(mode="json")


@pytest.fixture(scope="module")
def truth() -> GroundTruth:
    return load_truth(FIXTURE)


@pytest.fixture
def answer(truth: GroundTruth) -> dict[str, Any]:
    return copy.deepcopy(truth_answer(truth))


@pytest.fixture
def everything(truth: GroundTruth) -> ExpectedGroundTruth:
    return ExpectedGroundTruth(
        dead_code=tuple(entry.id for entry in truth.dead_code),
        data_flows=tuple(entry.id for entry in truth.data_flows),
        call_chains=tuple(entry.id for entry in truth.call_chains),
        dynamic_calls=tuple(entry.id for entry in truth.dynamic_calls),
    )


def _case(
    ground_truth: dict[str, Any] | None = HOOKS,
    grader: str = "ground_truth",
    fixture: str = FIXTURE,
    **expected: Any,
) -> Case:
    block: dict[str, Any] = {"verdict": None, **expected}
    if ground_truth is not None:
        block["ground_truth"] = ground_truth
    return Case.model_validate(
        {"id": "c", "fixture": fixture, "payload": {}, "expected": block, "grader": grader}
    )


def _no_judge(*_: object) -> JudgeVerdict:
    raise AssertionError("the judge must not be called")


def _dead(answer: dict[str, Any], symbol: str, kind: str = "function") -> None:
    answer["dead_code"].append(
        {
            "symbol": symbol,
            "kind": kind,
            "line": 1,
            "rule": "vulture",
            "confidence": 60,
            "evidence": "No call found.",
        }
    )


def _flow(answer: dict[str, Any], truth: GroundTruth, flow_id: str) -> dict[str, Any]:
    index = [flow.id for flow in truth.data_flows].index(flow_id)
    return answer["data_flows"][index]


# The case schema.


def test_the_grader_needs_the_block() -> None:
    with pytest.raises(ValidationError, match="ground_truth: required by the ground_truth grader"):
        _case(None)


@pytest.mark.parametrize("grader", ["llm_judge", "sandbox_tests", "jev", "incident"])
def test_other_graders_do_not_read_the_block(grader: str) -> None:
    with pytest.raises(ValidationError, match="expected"):
        _case(grader=grader)


def test_a_rubric_case_may_carry_the_block() -> None:
    case = _case(grader="rubric", rubric=RUBRIC)
    assert case.expected.ground_truth == ExpectedGroundTruth(dynamic_calls=("dynamic-hooks",))


@pytest.mark.parametrize(
    "block",
    [
        {},
        {"dead_code": []},
        {"dead_code": ["dead-class", "dead-class"]},
        {"decoys": ["decoy-callback"]},
    ],
)
def test_a_bad_block_is_refused(block: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="ground_truth"):
        _case(block)


def test_a_judge_never_sees_the_ids() -> None:
    assert "dynamic-hooks" not in _case().expected.model_dump_json()


# Loading.


def test_load_truth_reads_the_fixture_by_name(truth: GroundTruth) -> None:
    assert truth.fixture == FIXTURE


def test_load_truth_names_an_unknown_fixture() -> None:
    with pytest.raises(UsageError, match="no fixture named 'nothing-here'"):
        load_truth("nothing-here")


def test_load_truth_names_a_fixture_without_ground_truth() -> None:
    name = next(n for n, fixture in discover().items() if not fixture.ground_truth_path.exists())
    with pytest.raises(UsageError, match=f"fixture {name} has no ground-truth.yaml"):
        load_truth(name)


def test_grade_refuses_a_fixture_without_ground_truth(answer: dict[str, Any]) -> None:
    name = next(n for n, fixture in discover().items() if not fixture.ground_truth_path.exists())
    with pytest.raises(UsageError, match="has no ground-truth.yaml"):
        grade(_case(fixture=name), answer, _no_judge)


@pytest.mark.parametrize("section", list(ExpectedGroundTruth.model_fields))
def test_an_id_that_is_not_in_its_section_is_named(
    section: str, truth: GroundTruth, answer: dict[str, Any]
) -> None:
    expected = ExpectedGroundTruth.model_validate({section: ["no-such-id"]})
    with pytest.raises(UsageError, match=f"{section}: no such entry.*no-such-id"):
        check_ground_truth(expected, answer, truth)


# An answer that agrees, and one that says nothing.


def test_the_ground_truth_agrees_with_itself(
    truth: GroundTruth, answer: dict[str, Any], everything: ExpectedGroundTruth
) -> None:
    assert check_ground_truth(everything, answer, truth) == ()


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"dead_code": None, "data_flows": "none", "execution_paths": 3},
        {"dead_code": ["x"], "data_flows": [None], "execution_paths": [{"calls": "x"}]},
        {
            "dead_code": [{"symbol": 1}],
            "data_flows": [{"steps": [{"symbol": None}]}],
            "execution_paths": [{"calls": [{}, {"status": "unresolved", "expression": 4}]}],
        },
    ],
)
def test_a_malformed_answer_fails_every_entry_and_never_raises(
    broken: dict[str, Any], truth: GroundTruth, everything: ExpectedGroundTruth
) -> None:
    failures = check_ground_truth(everything, broken, truth)
    named = [i for ids in everything.model_dump().values() for i in ids]
    assert len(failures) == len(named)
    assert all(f" {i}: " in failure for i, failure in zip(named, failures, strict=True))


# Dead code and decoys.


def test_each_dead_entry_is_required(truth: GroundTruth, answer: dict[str, Any]) -> None:
    for entry in truth.dead_code:
        rest = [item for item in answer["dead_code"] if item["symbol"] != entry.symbol]
        failures = check_ground_truth(
            ExpectedGroundTruth(dead_code=(entry.id,)), answer | {"dead_code": rest}, truth
        )
        assert failures == (f"dead code {entry.id}: {entry.symbol} is not listed as dead",)


def test_a_dead_entry_needs_its_kind(truth: GroundTruth, answer: dict[str, Any]) -> None:
    entry = next(item for item in answer["dead_code"] if item["kind"] == "class")
    entry["kind"] = "function"
    (failure,) = check_ground_truth(ExpectedGroundTruth(dead_code=("dead-class",)), answer, truth)
    assert failure.startswith("dead code dead-class:")
    assert "is listed as 'function', it is a class" in failure


def test_the_order_and_the_rest_of_the_list_do_not_matter(
    truth: GroundTruth, answer: dict[str, Any], everything: ExpectedGroundTruth
) -> None:
    for field in ("dead_code", "data_flows", "execution_paths"):
        answer[field].reverse()
    _dead(answer, "legacy_billing/notify.py::not_in_the_truth")
    assert check_ground_truth(everything, answer, truth) == ()


def test_each_decoy_called_dead_fails_whatever_the_case_names(
    truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for decoy in truth.decoys:
        claimed = copy.deepcopy(answer)
        _dead(claimed, decoy.symbol)
        failures = check_ground_truth(
            ExpectedGroundTruth(dead_code=("dead-class",)), claimed, truth
        )
        assert [failure.split(":")[0] for failure in failures] == [f"decoy {decoy.id}"]
        assert decoy.symbol in failures[0]


def test_a_module_called_dead_calls_the_decoys_in_it_dead(
    truth: GroundTruth, answer: dict[str, Any]
) -> None:
    _dead(answer, "legacy_billing/exporters.py", "module")
    failures = check_ground_truth(ExpectedGroundTruth(dead_code=("dead-class",)), answer, truth)
    assert [failure.split(":")[0] for failure in failures] == [
        "decoy decoy-export-csv",
        "decoy decoy-export-fixed",
    ]


def test_a_longer_name_is_not_the_decoy(truth: GroundTruth, answer: dict[str, Any]) -> None:
    _dead(answer, "legacy_billing/exporters.py::export_csv_old")
    _dead(answer, "legacy_billing/export", "module")
    assert check_ground_truth(ExpectedGroundTruth(dead_code=("dead-class",)), answer, truth) == ()


def test_a_decoy_among_the_dynamic_lookups_is_no_claim_of_death(
    truth: GroundTruth, answer: dict[str, Any], everything: ExpectedGroundTruth
) -> None:
    answer["dynamic_lookups"] = [{"symbol": decoy.symbol} for decoy in truth.decoys]
    assert check_ground_truth(everything, answer, truth) == ()


# Data flows.


def test_each_data_flow_is_required(truth: GroundTruth, answer: dict[str, Any]) -> None:
    for index, flow in enumerate(truth.data_flows):
        rest = [item for i, item in enumerate(answer["data_flows"]) if i != index]
        (failure,) = check_ground_truth(
            ExpectedGroundTruth(data_flows=(flow.id,)), answer | {"data_flows": rest}, truth
        )
        assert failure.startswith(f"data flow {flow.id}: no data flow describes it")


def test_no_data_flows_at_all_is_said(truth: GroundTruth, answer: dict[str, Any]) -> None:
    answer["data_flows"] = []
    (failure,) = check_ground_truth(
        ExpectedGroundTruth(data_flows=("flow-period-to-ledger",)), answer, truth
    )
    assert failure == "data flow flow-period-to-ledger: the answer has no data flows"


def test_a_flow_may_miss_one_step_and_pass_more(truth: GroundTruth, answer: dict[str, Any]) -> None:
    flow_id = "flow-overdue-to-ledger"
    steps = _flow(answer, truth, flow_id)["steps"]
    removed = next(step for step in steps if step["symbol"].endswith("::late_fee_for"))
    steps.remove(removed)
    steps.insert(1, {"symbol": "legacy_billing/state.py::today", "does": "gives the day"})
    expected = ExpectedGroundTruth(data_flows=(flow_id,))
    assert check_ground_truth(expected, answer, truth) == ()

    second = next(step for step in steps if step["symbol"].endswith("::overdue_invoices"))
    steps.remove(second)
    (failure,) = check_ground_truth(expected, answer, truth)
    assert flow_id in failure
    assert removed["symbol"] in failure
    assert second["symbol"] in failure


def test_a_flow_may_not_miss_a_module(truth: GroundTruth, answer: dict[str, Any]) -> None:
    flow_id = "flow-country-to-total"
    steps = _flow(answer, truth, flow_id)["steps"]
    steps[:] = [step for step in steps if "taxes.py" not in step["symbol"]]
    (failure,) = check_ground_truth(ExpectedGroundTruth(data_flows=(flow_id,)), answer, truth)
    assert "module legacy_billing/taxes.py is missing" in failure


def test_a_flow_of_two_steps_may_miss_none(truth: GroundTruth, answer: dict[str, Any]) -> None:
    flow_id = "flow-period-to-ledger"
    steps = _flow(answer, truth, flow_id)["steps"]
    assert len(steps) == 2
    answer["data_flows"] = [_flow(answer, truth, flow_id)]
    steps[1] = {"symbol": "legacy_billing/ledger.py::entries", "does": "reads it back"}
    (failure,) = check_ground_truth(ExpectedGroundTruth(data_flows=(flow_id,)), answer, truth)
    assert "step legacy_billing/ledger.py::post_entry is missing" in failure


def test_a_flow_told_backwards_fails(truth: GroundTruth, answer: dict[str, Any]) -> None:
    flow_id = "flow-usage-to-invoice"
    _flow(answer, truth, flow_id)["steps"].reverse()
    (failure,) = check_ground_truth(ExpectedGroundTruth(data_flows=(flow_id,)), answer, truth)
    assert "legacy_billing/importer.py comes after legacy_billing/billing.py" in failure


def test_the_prose_of_a_flow_is_not_compared(
    truth: GroundTruth, answer: dict[str, Any], everything: ExpectedGroundTruth
) -> None:
    for flow in answer["data_flows"]:
        flow["data"] = "Something else."
        for step in flow["steps"]:
            step["does"] = "Other words."
    assert check_ground_truth(everything, answer, truth) == ()


# Call chains.


def test_each_call_of_a_chain_is_required(truth: GroundTruth, answer: dict[str, Any]) -> None:
    for chain in truth.call_chains:
        for call in chain.calls:
            short = copy.deepcopy(answer)
            for path in short["execution_paths"]:
                path["calls"] = [
                    item
                    for item in path["calls"]
                    if (item["caller"], item["callee"]) != (call.caller, call.callee)
                ]
            (failure,) = check_ground_truth(
                ExpectedGroundTruth(call_chains=(chain.id,)), short, truth
            )
            assert failure == (
                f"call chain {chain.id}: not in the execution paths: "
                f"{call.caller} calls {call.callee}"
            )


def test_a_chain_may_be_spread_over_paths(truth: GroundTruth, answer: dict[str, Any]) -> None:
    calls = [call for path in answer["execution_paths"] for call in path["calls"]]
    answer["execution_paths"] = [{"title": "one call", "calls": [call]} for call in calls]
    expected = ExpectedGroundTruth(call_chains=tuple(chain.id for chain in truth.call_chains))
    assert check_ground_truth(expected, answer, truth) == ()


def test_an_unresolved_call_is_no_call_of_a_chain(
    truth: GroundTruth, answer: dict[str, Any]
) -> None:
    call = answer["execution_paths"][0]["calls"][0]
    call["status"] = "unresolved"
    failures = check_ground_truth(
        ExpectedGroundTruth(call_chains=("chain-billing-run",)), answer, truth
    )
    assert any(call["caller"] in failure for failure in failures)


# Dynamic calls.


def test_each_dynamic_call_is_required(truth: GroundTruth, answer: dict[str, Any]) -> None:
    for entry in truth.dynamic_calls:
        short = copy.deepcopy(answer)
        for path in short["execution_paths"]:
            path["calls"] = [
                call
                for call in path["calls"]
                if call["status"] == "resolved" or call["caller"] != entry.symbol
            ]
        (failure,) = check_ground_truth(
            ExpectedGroundTruth(dynamic_calls=(entry.id,)), short, truth
        )
        assert failure.startswith(f"dynamic call {entry.id}:")
        assert failure.endswith("is not reported as an unresolved call")


def test_a_dynamic_call_reported_as_a_plain_call_fails_whatever_the_case_names(
    truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for entry in truth.dynamic_calls:
        for target in entry.targets:
            guessed = copy.deepcopy(answer)
            guessed["execution_paths"][0]["calls"].append(
                {"status": "resolved", "caller": entry.symbol, "callee": target}
            )
            failures = check_ground_truth(
                ExpectedGroundTruth(dead_code=("dead-class",)), guessed, truth
            )
            assert failures == (
                (
                    f"dynamic call {entry.id}: reported as a plain call from {entry.symbol} "
                    f"to {target}"
                ),
            )


@pytest.mark.parametrize(
    ("expression", "found"),
    [
        ("hook(row)", True),
        ("hook( row )", True),
        ("hook(...)", True),
        ("for hook in HOOKS: hook(row)", True),
        ("hooks(row)", False),
        ("other(hooks)", False),
        ("", False),
        (None, False),
    ],
)
def test_the_expression_of_a_dynamic_call(
    expression: str | None, found: bool, truth: GroundTruth, answer: dict[str, Any]
) -> None:
    for path in answer["execution_paths"]:
        for call in path["calls"]:
            if call["status"] == "unresolved" and call["caller"] == POST_INVOICE:
                call["expression"] = expression
    expected = ExpectedGroundTruth(dynamic_calls=("dynamic-hooks",))
    assert (check_ground_truth(expected, answer, truth) == ()) is found


# The merge into `grade`.


def test_passing_checks_pass_without_a_judge(answer: dict[str, Any]) -> None:
    result = grade(_case(), answer, _no_judge)
    assert (result.passed, result.score, result.judge) == (True, 1.0, None)
    assert result.reason == "the ground truth checks passed"


def test_a_failed_check_is_the_reason(answer: dict[str, Any]) -> None:
    _dead(answer, "legacy_billing/audit.py::log_posting")
    result = grade(_case(), answer, _no_judge)
    assert (result.passed, result.score, result.judge) == (False, 0.0, None)
    assert "decoy decoy-callback" in result.reason


@pytest.mark.parametrize("spoil", [None, _dead])
def test_a_rubric_case_is_judged_only_when_the_checks_pass(
    spoil: Callable[[dict[str, Any], str], None] | None, answer: dict[str, Any]
) -> None:
    case = _case(grader="rubric", rubric=RUBRIC)
    asked: list[object] = []

    def judge(*args: object) -> JudgeVerdict:
        asked.append(args)
        return JudgeVerdict(passed=True, score=0.9, reason="clear")

    if spoil is not None:
        spoil(answer, "legacy_billing/audit.py::log_posting")
    result = grade(case, answer, _no_judge, rubric_judge=judge)
    assert result.passed is (spoil is None)
    assert len(asked) == (1 if spoil is None else 0)


# Features, which a blueprint answers instead of an archaeology output.

PARTS = {
    "symbols": ("symbols", "symbol", "is missing from symbols"),
    "uses": ("uses", "symbol", "is missing from uses"),
    "called_from": ("called_from", "caller", "is missing from the callers of called_from"),
    "state": ("state", "symbol", "is missing from state"),
    "tests": ("pinning_tests", "path", "is missing from pinning_tests"),
}


@pytest.fixture(params=["feature-late-fees", "feature-discounts"])
def feature(request: pytest.FixtureRequest, truth: GroundTruth) -> Feature:
    return next(entry for entry in truth.features if entry.id == request.param)


@pytest.fixture
def blueprint(feature: Feature) -> dict[str, Any]:
    return truth_blueprint(feature)


def _features(feature: Feature, blueprint: object, truth: GroundTruth) -> tuple[str, ...]:
    assert isinstance(blueprint, dict)
    return check_ground_truth(ExpectedGroundTruth(features=(feature.id,)), blueprint, truth)


def _wanted(feature: Feature, part: str) -> list[str]:
    """Return what `feature` lists under `part`, a site by its symbol."""
    entries = getattr(feature, part)
    return [entry if isinstance(entry, str) else entry.symbol for entry in entries]


def test_the_parametrized_features_are_all_of_them(truth: GroundTruth) -> None:
    assert [entry.id for entry in truth.features] == ["feature-late-fees", "feature-discounts"]


def test_a_blueprint_built_from_the_ground_truth_agrees(
    feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    assert _features(feature, blueprint, truth) == ()


def test_a_blueprint_passes_the_checks_that_need_no_id(
    blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    """It lists no dead code and no calls, so the decoy and the not-guessed checks find nothing."""
    assert not {"dead_code", "execution_paths"} & set(ExtractionBlueprint.model_fields)
    assert check_ground_truth(ExpectedGroundTruth.model_construct(), blueprint, truth) == ()


def test_the_blueprint_of_another_feature_fails(
    feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    other = next(entry for entry in truth.features if entry.id != feature.id)
    (failure,) = _features(other, blueprint, truth)
    assert failure.startswith(f"feature {other.id}: ")


@pytest.mark.parametrize("part", [part for part in PARTS if part != "uses"])
def test_each_entry_of_a_feature_is_required(
    part: str, feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    field, key, message = PARTS[part]
    wanted = _wanted(feature, part)
    assert wanted
    for value in wanted:
        rest = [item for item in blueprint[field] if item[key] != value]
        failures = _features(feature, blueprint | {field: rest}, truth)
        assert failures == (f"feature {feature.id}: {value} {message}",)


def test_uses_may_miss_one_symbol_and_not_two(
    feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    first, second, *_ = feature.uses
    for gone in feature.uses:
        rest = [item for item in blueprint["uses"] if item["symbol"] != gone]
        assert _features(feature, blueprint | {"uses": rest}, truth) == ()
    rest = [item for item in blueprint["uses"] if item["symbol"] not in (first, second)]
    assert _features(feature, blueprint | {"uses": rest}, truth) == (
        f"feature {feature.id}: {first} is missing from uses; {second} is missing from uses",
    )


def test_everything_a_blueprint_misses_is_in_one_failure(
    feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    (failure,) = _features(feature, blueprint | {"state": [], "pinning_tests": []}, truth)
    named = [*_wanted(feature, "state"), *_wanted(feature, "tests")]
    assert all(f"{value} is missing from " in failure for value in named)


@pytest.mark.parametrize("part", ["uses", "called_from"])
def test_what_is_outside_a_feature_may_not_be_in_its_symbols(
    part: str, feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    for symbol in _wanted(feature, part):
        symbols = [*blueprint["symbols"], {"symbol": symbol, "role": "Part of it."}]
        (failure,) = _features(feature, blueprint | {"symbols": symbols}, truth)
        claim = f"{symbol} is listed in symbols, but {symbol} is outside the feature"
        assert failure == f"feature {feature.id}: {claim}"


@pytest.mark.parametrize(
    "claim", ["legacy_billing/money.py", "legacy_billing/invoices.py::Invoice"]
)
def test_a_module_or_a_class_holds_what_is_outside_a_feature(
    claim: str, truth: GroundTruth
) -> None:
    feature = next(entry for entry in truth.features if entry.id == "feature-discounts")
    blueprint = truth_blueprint(feature)
    blueprint["symbols"].append({"symbol": claim, "role": "Part of it."})
    (failure,) = _features(feature, blueprint, truth)
    assert f"{claim} is listed in symbols, but {claim}" in failure


def test_a_class_is_not_the_method_a_feature_uses(truth: GroundTruth) -> None:
    feature = next(entry for entry in truth.features if entry.id == "feature-discounts")
    blueprint = truth_blueprint(feature)
    for item in blueprint["uses"]:
        if "::Invoice." in item["symbol"]:
            item["symbol"] = "legacy_billing/invoices.py::Invoice"
    (failure,) = _features(feature, blueprint, truth)
    assert "legacy_billing/invoices.py::Invoice.charges is missing from uses" in failure


@pytest.mark.parametrize("part", list(PARTS))
def test_other_extra_entries_are_not_compared(
    part: str, feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    field, key, _ = PARTS[part]
    blueprint[field] = [{**blueprint[field][0], key: "legacy_billing/reports.py::x"}] + (
        blueprint[field][::-1]
    )
    assert _features(feature, blueprint, truth) == ()


def test_the_prose_of_a_blueprint_is_not_compared(
    feature: Feature, blueprint: dict[str, Any], truth: GroundTruth
) -> None:
    blueprint |= {"summary": "Other.", "invariants": ["Other."], "inputs": [], "outputs": []}
    assert _features(feature, blueprint, truth) == ()


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"symbols": None, "uses": "none", "called_from": 3, "state": {}, "pinning_tests": True},
        {"symbols": ["x"], "uses": [None], "called_from": [{"caller": 1}], "state": [{}]},
        {"symbols": [{"symbol": None}], "pinning_tests": [{"path": ["tests"]}]},
    ],
)
def test_a_malformed_blueprint_fails_and_never_raises(
    broken: dict[str, Any], feature: Feature, truth: GroundTruth
) -> None:
    (failure,) = _features(feature, broken, truth)
    assert failure.startswith(f"feature {feature.id}: ")
