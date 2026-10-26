"""Tests for evals/graders/jev.py and for evaluating a Jev agent, with fake Jev and fake judges."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import evals.graders.llm_judge as llm_judge_module
import evals.run as eval_run
from evals.cases import CASES_FILE, Case, Expected, load_cases
from evals.graders import grade
from evals.graders.jev import (
    GRADER_MANIFEST,
    Agreement,
    JevJudge,
    JevVerdict,
    _Both,
    compare,
    load_grader_spec,
    main,
    threshold_of,
)
from evals.graders.llm_judge import JudgeVerdict
from evals.run import evaluate
from factory.exit import AuthOrConfigError, UsageError
from registry.jev_spec import JevOutput, JevSpec
from registry.resolver import resolve
from runtime.budget import Budget
from runtime.jev_runner import JevError, JevTransportError, NoulResult
from runtime.trace import Trace
from tests.pipelines.test_review_panel import GATE_ANSWERS
from tests.runtime.test_jev_runner import FakeTransport, response

EXPECTED = Expected(verdict="ship", criteria="All reviewers approve.")


class FakeLLM:
    """A fallback judge that returns a fixed verdict and counts its calls."""

    def __init__(self, passed: bool = True, score: float = 0.8) -> None:
        self.verdict = JudgeVerdict(passed=passed, score=score, reason="llm judged")
        self.calls = 0

    def __call__(self, expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        self.calls += 1
        return self.verdict


def matches(p: float) -> FakeTransport:
    return FakeTransport(response({"matches": NoulResult(noul=p)}))


def tags(trace: Trace) -> list[str | None]:
    trace.ensure_schema()
    return [row[0] for row in trace._connect().execute("SELECT tag FROM traces").fetchall()]


@pytest.fixture
def trace(tmp_path: Path) -> Trace:
    return Trace(tmp_path / "traces.duckdb")


def test_the_grader_manifest_loads_and_has_a_threshold() -> None:
    spec = load_grader_spec()
    assert spec.name == "eval-grader-jev"
    assert GRADER_MANIFEST.name == "jev_grader.yaml"
    assert threshold_of(spec) == 0.5


def test_the_grader_is_not_in_the_registry() -> None:
    from registry.resolver import list_agents

    assert "eval-grader-jev" not in {found.name for found in list_agents()}


def test_threshold_of_rejects_a_manifest_without_the_gate() -> None:
    spec = load_grader_spec()
    stripped = spec.model_copy(update={"gates": ()})
    with pytest.raises(ValueError, match="needs a `matches` gate"):
        threshold_of(stripped)


def test_an_answer_over_the_threshold_passes(trace: Trace) -> None:
    verdict = JevJudge(trace, FakeLLM(passed=False), transport=matches(0.9))(EXPECTED, {"a": 1})
    assert verdict.passed
    assert verdict.score == 0.9
    assert not verdict.grader_fallback
    assert "p(matches) 0.90" in verdict.reason


def test_an_answer_at_the_threshold_fails(trace: Trace) -> None:
    verdict = JevJudge(trace, FakeLLM(), transport=matches(0.5))(EXPECTED, {"a": 1})
    assert not verdict.passed
    assert verdict.score == 0.5


def test_the_fallback_is_not_asked_when_jev_answers(trace: Trace) -> None:
    fallback = FakeLLM()
    JevJudge(trace, fallback, transport=matches(0.9))(EXPECTED, {"a": 1})
    assert fallback.calls == 0


def test_the_state_holds_the_expectation_and_the_answer(trace: Trace) -> None:
    transport = matches(0.9)
    JevJudge(trace, FakeLLM(), transport=transport)(EXPECTED, {"verdict": "ship"})
    (call,) = transport.calls
    assert '"verdict": "ship"' in call["state"]
    assert "All reviewers approve." in call["state"]
    assert "Answer of the agent" in call["state"]


def test_the_trace_row_carries_the_tag_and_the_pipeline_run(trace: Trace) -> None:
    JevJudge(trace, FakeLLM(), tag="eval:x@1.0.0", pipeline_run_id="run-1", transport=matches(0.9))(
        EXPECTED, {"a": 1}
    )
    assert tags(trace) == ["eval:x@1.0.0"]
    assert trace.cost_by_pipeline_run("run-1") > 0


def test_a_jev_failure_falls_back_and_says_so(trace: Trace) -> None:
    transport = FakeTransport(JevTransportError("bad request", status=400))
    fallback = FakeLLM(passed=True, score=0.6)
    verdict = JevJudge(trace, fallback, transport=transport)(EXPECTED, {"a": 1})
    assert verdict.grader_fallback
    assert verdict.passed
    assert verdict.score == 0.6
    assert verdict.reason.startswith("jev grader failed (")
    assert verdict.reason.endswith("llm judged")
    assert fallback.calls == 1


def test_a_failed_key_is_not_a_fallback(trace: Trace) -> None:
    class Unauthorised:
        def ask(self, **_: Any) -> Any:
            raise AuthOrConfigError("no key")

    fallback = FakeLLM()
    judge = JevJudge(trace, fallback, transport=Unauthorised())
    with pytest.raises(AuthOrConfigError):
        judge(EXPECTED, {"a": 1})
    assert fallback.calls == 0


def test_a_jev_verdict_serialises_like_a_judge_verdict() -> None:
    verdict = JevVerdict(passed=True, score=0.9, reason="ok")
    assert not verdict.grader_fallback
    assert verdict.passed


def test_grading_a_jev_case_without_a_jev_judge_is_a_usage_error() -> None:
    case = _jev_case()
    with pytest.raises(UsageError, match="needs a jev_judge"):
        grade(case, {"verdict": "ship"}, FakeLLM())


def test_a_jev_case_goes_to_the_jev_judge_and_others_to_the_judge() -> None:
    llm, jev = FakeLLM(), FakeLLM()
    graded = grade(_jev_case(), {"verdict": "ship"}, llm, jev_judge=jev)
    assert graded.passed
    assert (llm.calls, jev.calls) == (0, 1)
    other = _jev_case().model_copy(update={"grader": "llm_judge"})
    grade(other, {"verdict": "ship"}, llm, jev_judge=jev)
    assert (llm.calls, jev.calls) == (1, 1)


def test_a_failed_check_never_reaches_the_jev_judge() -> None:
    jev = FakeLLM()
    graded = grade(_jev_case(), {"verdict": "unclear"}, FakeLLM(), jev_judge=jev)
    assert not graded.passed
    assert jev.calls == 0


def _jev_case(case_id: str = "c1") -> Case:
    return Case(id=case_id, fixture="clean-refactor", expected=EXPECTED, grader="jev")


# --- evaluating a Jev agent -------------------------------------------------------------------


class FakeJevRunner:
    """Stands in for `run_jev` in `evaluate`: answers per call, records the keyword arguments."""

    def __init__(self, spec: JevSpec, answers: Mapping[str, Any] = GATE_ANSWERS) -> None:
        self.spec = spec
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, spec: JevSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> JevOutput:
        self.calls.append({"payload": dict(payload), **kwargs})
        return spec.output_class.model_validate(self.answers)


SHIP = {
    "clear_cut": {"p": 0.95},
    "verdict": {
        "answer": "ship",
        "probs": {"ship": 0.95, "request_changes": 0.03, "unclear": 0.02},
        "confidence": 0.95,
    },
}


@pytest.fixture(scope="module")
def gate_spec() -> JevSpec:
    found = resolve("judge-gate", "^0.1")
    assert isinstance(found, JevSpec)
    return found


@pytest.fixture(scope="module")
def gate_cases(gate_spec: JevSpec) -> list[Case]:
    return load_cases(gate_spec.directory / CASES_FILE)


@pytest.fixture
def paths(tmp_path: Path, gate_cases: list[Case]) -> dict[str, Path]:
    return {case.fixture: tmp_path / case.fixture for case in gate_cases}


def test_a_jev_agent_is_evaluated_with_its_answers_flattened(
    gate_spec: JevSpec, gate_cases: list[Case], trace: Trace, paths: dict[str, Path]
) -> None:
    runner = FakeJevRunner(gate_spec, SHIP)
    seen: list[dict[str, Any]] = []

    def jev_judge(expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        seen.append(dict(actual))
        return JudgeVerdict(passed=True, score=0.9, reason="matched")

    report = evaluate(
        gate_spec,
        gate_cases,
        trace,
        jev_runner=runner,
        jev_judge=jev_judge,
        judge=FakeLLM(),
        fixture_paths=paths,
        workers=1,
    )
    by_id = {result.case.id: result for result in report.results}
    assert by_id["clean-refactor-ships"].passed
    assert seen and seen[0] == {"clear_cut": 0.95, "verdict": "ship"}
    # Every case that pins a verdict other than `ship` fails the deterministic check before any
    # judge is asked; a case with no pinned verdict goes to the judge, which here approves.
    failed = [
        result for result in report.results if result.case.expected.verdict not in {"ship", None}
    ]
    assert failed
    assert all(not result.passed for result in failed)
    assert all(
        "verdict is 'ship'" in (result.grade.reason if result.grade else "") for result in failed
    )
    assert len(seen) == len(report.results) - len(failed)


def test_each_jev_call_gets_the_payload_budget_tag_and_run_id(
    gate_spec: JevSpec, gate_cases: list[Case], trace: Trace, paths: dict[str, Path]
) -> None:
    runner = FakeJevRunner(gate_spec, SHIP)
    budget = Budget(5.0)
    report = evaluate(
        gate_spec,
        gate_cases,
        trace,
        jev_runner=runner,
        jev_judge=FakeLLM(),
        judge=FakeLLM(),
        fixture_paths=paths,
        workers=1,
        budget=budget,
        eval_run_id="eval-run-1",
    )
    assert len(runner.calls) == len(gate_cases)
    for call in runner.calls:
        assert call["budget"] is budget
        assert call["tag"] == "eval:judge-gate@0.1.0"
        assert call["pipeline_run_id"] == "eval-run-1"
        assert "reviews" in call["payload"]
        assert "repo_path" not in call["payload"]
    assert report.tag == "eval:judge-gate@0.1.0"


def test_the_report_marks_a_case_graded_by_the_fallback(
    gate_spec: JevSpec, gate_cases: list[Case], trace: Trace, paths: dict[str, Path]
) -> None:
    fell_back = JevVerdict(
        passed=True, score=0.6, reason="jev grader failed (x); ok", grader_fallback=True
    )
    report = evaluate(
        gate_spec,
        gate_cases[:1],
        trace,
        jev_runner=FakeJevRunner(gate_spec, SHIP),
        jev_judge=lambda expected, actual: fell_back,
        judge=FakeLLM(),
        fixture_paths=paths,
    )
    (case,) = report.to_json()["cases"]
    assert case["grader_fallback"] is True
    assert case["grader"] == "jev"


def test_a_case_not_graded_by_the_fallback_says_so(
    gate_spec: JevSpec, gate_cases: list[Case], trace: Trace, paths: dict[str, Path]
) -> None:
    report = evaluate(
        gate_spec,
        gate_cases[:1],
        trace,
        jev_runner=FakeJevRunner(gate_spec, SHIP),
        jev_judge=FakeLLM(),
        judge=FakeLLM(),
        fixture_paths=paths,
    )
    assert report.to_json()["cases"][0]["grader_fallback"] is False


def test_a_jev_failure_in_the_agent_is_a_failed_case(
    gate_spec: JevSpec, gate_cases: list[Case], trace: Trace, paths: dict[str, Path]
) -> None:
    def broken(*_: Any, **__: Any) -> JevOutput:
        raise JevError("the run failed")

    report = evaluate(
        gate_spec,
        gate_cases[:1],
        trace,
        jev_runner=broken,
        jev_judge=FakeLLM(),
        judge=FakeLLM(),
        fixture_paths=paths,
    )
    (result,) = report.results
    assert not result.passed
    assert result.error == "JevError: the run failed"


def test_a_default_jev_judge_falls_back_to_the_given_judge(
    gate_spec: JevSpec, gate_cases: list[Case], trace: Trace, paths: dict[str, Path]
) -> None:
    llm = FakeLLM()
    # No jev_judge: evaluate builds a JevJudge over `llm`. Its transport is the default one, which
    # needs a key; clear it so the run is an `AuthOrConfigError`, which is not a fallback.
    with pytest.raises(AuthOrConfigError):
        _without_key(
            lambda: evaluate(
                gate_spec,
                gate_cases[:1],
                trace,
                jev_runner=FakeJevRunner(gate_spec, SHIP),
                judge=llm,
                fixture_paths=paths,
            )
        )
    assert llm.calls == 0


def _without_key(call: Any) -> Any:
    import os

    from runtime.jev_runner import API_KEY_ENV

    saved = os.environ.pop(API_KEY_ENV, None)
    try:
        return call()
    finally:
        if saved is not None:
            os.environ[API_KEY_ENV] = saved


# --- agreement ----------------------------------------------------------------------------------


def test_agreement_counts_pairs() -> None:
    agreement = Agreement(
        "a@1.0.0", [(True, True, False), (True, False, False), (False, False, True)]
    )
    assert agreement.compared == 3
    assert agreement.agreed == 2
    assert agreement.fallbacks == 1
    assert agreement.to_json() == {
        "agent": "a@1.0.0",
        "compared": 3,
        "agreed": 2,
        "agreement": 2 / 3,
        "fallbacks": 1,
    }


def test_an_empty_agreement_has_no_rate() -> None:
    assert Agreement("a@1.0.0").to_json()["agreement"] is None


def test_both_asks_both_graders_and_returns_the_llm_verdict() -> None:
    agreement = Agreement("a@1.0.0")
    llm = FakeLLM(passed=True)
    jev = FakeLLM(passed=False)
    both = _Both(llm, jev, agreement)
    verdict = both(EXPECTED, {"a": 1})
    assert verdict is llm.verdict
    assert agreement.pairs == [(True, False, False)]


def test_both_records_a_fallback() -> None:
    agreement = Agreement("a@1.0.0")
    fell = JevVerdict(passed=True, score=0.5, reason="r", grader_fallback=True)
    both = _Both(FakeLLM(), lambda expected, actual: fell, agreement)
    both(EXPECTED, {"a": 1})
    assert agreement.pairs == [(True, True, True)]


class _FakeEvaluate:
    """Replaces `evals.run.evaluate`: grades two answers through the judge it is given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, spec: Any, cases: Any, trace: Any, **kwargs: Any) -> None:
        self.calls.append({"spec": spec, "cases": cases, **kwargs})
        assert kwargs["judge"] is kwargs["jev_judge"]
        for _ in range(2):
            kwargs["judge"](EXPECTED, {"verdict": "ship"})


class _ClosableLLM(FakeLLM):
    def __init__(self, trace: Trace, **_: Any) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_compare_grades_every_answer_with_both_graders(
    monkeypatch: pytest.MonkeyPatch, trace: Trace
) -> None:
    fake = _FakeEvaluate()
    monkeypatch.setattr(eval_run, "evaluate", fake)
    monkeypatch.setattr(llm_judge_module, "LLMJudge", _ClosableLLM)
    monkeypatch.setattr(
        JevJudge,
        "__call__",
        lambda self, expected, actual: JevVerdict(passed=False, score=0.2, reason="no"),
    )
    (agreement,) = compare(["judge-gate@^0.1"], trace)
    assert agreement.agent == "judge-gate@0.1.0"
    assert agreement.compared == 2
    assert agreement.agreed == 0
    assert fake.calls[0]["workers"] == 1


def test_main_prints_the_agreement_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "t.duckdb"))
    monkeypatch.setattr(
        "evals.graders.jev.compare",
        lambda refs, trace: [Agreement("a@1.0.0", [(True, True, False)])],
    )
    assert main(["a@^1.0", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == [
        {"agent": "a@1.0.0", "compared": 1, "agreed": 1, "agreement": 1.0, "fallbacks": 0}
    ]


def test_main_prints_a_line_per_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "t.duckdb"))
    monkeypatch.setattr(
        "evals.graders.jev.compare",
        lambda refs, trace: [Agreement("a@1.0.0", [(True, True, False), (True, False, True)])],
    )
    assert main(["a"]) == 0
    assert capsys.readouterr().out.strip() == "a@1.0.0: 1/2 agree (50%), 1 fell back"


def test_main_defaults_to_every_agent_that_has_cases_and_no_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from evals.graders.jev import _default_refs

    refs = _default_refs()
    assert "judge-gate@0.1.0" in refs
    assert "code-reviewer@1.2.0" in refs or any(ref.startswith("code-reviewer@") for ref in refs)
    assert all("@" in ref for ref in refs)
