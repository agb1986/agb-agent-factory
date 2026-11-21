"""Tests for the Jev side of evals/run.py: `$fixture` payloads, expected answers and decisions,
and the `recall_risky`, `skip_rate` and `escalation_rate` metrics with their minimum."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import evals.run as eval_run
from evals.cases import CASES_FILE, Case, CaseFileError, DecisionName, Expected, load_cases
from evals.graders import check_deterministic
from evals.graders.llm_judge import JudgeVerdict
from evals.run import CaseResult, EvalReport, evaluate
from fixtures.build import build
from registry.jev_spec import JevOutput, JevSpec
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.trace import Trace, TraceRow
from tools import MAX_OUTPUT_BYTES
from tools.git import show_diff


def triage_answers(p: float, choice: str = "code", confidence: float = 0.9) -> dict[str, Any]:
    probs = dict.fromkeys(("code", "code-security", "code-perf", "all"), (1 - confidence) / 3)
    probs[choice] = confidence
    return {
        "needs_review": {"p": p},
        "reviewers": {"answer": choice, "probs": probs, "confidence": confidence},
    }


# With the diff-triage gates: p below 0.5 skips, a confidence below 0.6 escalates.
SKIP = triage_answers(0.1)
REVIEW = triage_answers(0.9)
SECURITY = triage_answers(0.9, "code-security")
UNSURE = triage_answers(0.9, confidence=0.4)


class FakeJevRunner:
    """Stands in for `run_jev`: answers by the payload's `diff`, records every payload.

    `cut` names the diffs whose run writes a trace row saying the input was truncated, as
    `run_jev` does when the rendered input is over `max_chars`.
    """

    def __init__(self, answers: Mapping[str, Any], *, cut: frozenset[str] = frozenset()) -> None:
        self.answers = answers
        self.cut = cut
        self.payloads: list[dict[str, Any]] = []

    def __call__(
        self, spec: JevSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> JevOutput:
        self.payloads.append(dict(payload))
        diff = payload["diff"]
        if diff in self.cut:
            trace.record(
                TraceRow(
                    run_id="r",
                    agent_ref=spec.address,
                    kind="jev",
                    started_at=datetime.now(UTC),
                    duration_ms=1,
                    input_json={},
                    decision_json={"truncated": True},
                    pipeline_run_id=kwargs.get("pipeline_run_id"),
                    tag=kwargs.get("tag"),
                )
            )
        return spec.output_class.model_validate(self.answers.get(diff, REVIEW))


def approve(expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
    return JudgeVerdict(passed=True, score=1.0, reason="judged")


@pytest.fixture(scope="module")
def triage() -> JevSpec:
    spec = resolve("diff-triage", "^0.4")
    assert isinstance(spec, JevSpec)
    return spec


@pytest.fixture
def trace(tmp_path: Path) -> Trace:
    return Trace(tmp_path / "traces.duckdb")


def case(
    case_id: str,
    *,
    decision: DecisionName | None = "review",
    risky: bool = False,
    answers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    fixture: str = "docs-only",
) -> Case:
    return Case(
        id=case_id,
        fixture=fixture,
        payload={"files": "a.py", "diff": case_id} if payload is None else payload,
        expected=Expected(verdict=None, answers=answers, decision=decision, risky=risky),
        grader="jev",
    )


def run(
    spec: JevSpec | LLMSpec,
    cases: list[Case],
    trace: Trace,
    tmp_path: Path,
    runner: FakeJevRunner,
    paths: Mapping[str, Path] | None = None,
) -> EvalReport:
    return evaluate(
        spec,
        cases,
        trace,
        jev_runner=runner,
        jev_judge=approve,
        judge=approve,
        fixture_paths=paths or {c.fixture: tmp_path / c.fixture for c in cases},
        workers=1,
        eval_run_id="eval-run-1",
    )


# --- `$fixture` payload values


def test_fixture_references_are_replaced_by_the_diff_and_its_files(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    (built,) = build(["docs-only"], dest=tmp_path / "repos")
    refs = {"files": {"$fixture": "files"}, "diff": {"$fixture": "diff"}}
    runner = FakeJevRunner({})
    report = run(
        triage, [case("c", payload=refs)], trace, tmp_path, runner, {"docs-only": built.path}
    )
    (payload,) = runner.payloads
    diff = show_diff(built.path, "base", "head", max_bytes=MAX_OUTPUT_BYTES)
    assert payload["diff"] == diff
    assert payload["files"] == "README.md" or payload["files"].split("\n")
    assert all(f"b/{name}" in diff for name in payload["files"].split("\n"))
    assert report.results[0].passed


def test_a_payload_without_references_is_passed_unchanged(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    runner = FakeJevRunner({})
    run(triage, [case("c", payload={"files": "x.py", "diff": "plain"})], trace, tmp_path, runner)
    assert runner.payloads == [{"files": "x.py", "diff": "plain"}]


def test_a_cut_diff_escalates(
    triage: JevSpec, trace: Trace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cut = "diff --git a/x.py b/x.py\n+x\n[truncated: 10 bytes dropped, limit 5 bytes]"
    monkeypatch.setattr(eval_run, "show_diff", lambda *args, **kwargs: cut)
    runner = FakeJevRunner({cut: SKIP})
    refs = {"files": {"$fixture": "files"}, "diff": {"$fixture": "diff"}}
    report = run(triage, [case("c", payload=refs, decision="escalate")], trace, tmp_path, runner)
    assert runner.payloads[0]["files"] == "x.py"
    assert report.results[0].decision == "escalate"
    assert report.results[0].passed


def test_an_input_the_jev_runner_cut_escalates(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    runner = FakeJevRunner({"c": SKIP}, cut=frozenset({"c"}))
    report = run(triage, [case("c", decision="skip")], trace, tmp_path, runner)
    (result,) = report.results
    assert result.decision == "escalate"
    assert not result.passed
    assert result.grade is not None
    assert "decision is 'escalate', expected 'skip'" in result.grade.reason


@pytest.mark.parametrize("ref", [{"$fixture": "tree"}, {"$fixture": "diff", "extra": 1}])
def test_a_bad_fixture_reference_is_a_case_file_error(tmp_path: Path, ref: Any) -> None:
    import yaml

    path = tmp_path / CASES_FILE
    path.parent.mkdir()
    document = {
        "cases": [
            {
                "id": "c",
                "fixture": "docs-only",
                "payload": {"diff": ref},
                "expected": {"verdict": None},
                "grader": "jev",
            }
        ]
    }
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(CaseFileError, match=r"payload\.diff"):
        load_cases(path)


# --- expected answers and decisions


def test_answers_and_decision_are_checked_like_the_verdict(triage: JevSpec) -> None:
    expected = Expected(verdict=None, answers={"reviewers": "code-security"}, decision="review")
    output = triage.output_class.model_validate(SECURITY)
    actual = eval_run._jev_answers(output) | {"decision": "review"}
    assert check_deterministic(expected, actual).passed
    wrong = eval_run._jev_answers(triage.output_class.model_validate(REVIEW)) | {"decision": "skip"}
    failures = check_deterministic(expected, wrong).failures
    assert failures == (
        "reviewers is 'code', expected 'code-security'",
        "decision is 'skip', expected 'review'",
    )


@pytest.mark.parametrize(
    ("answers", "decision"), [(SKIP, "skip"), (REVIEW, "review"), (UNSURE, "escalate")]
)
def test_the_decision_names_what_decide_returned(
    triage: JevSpec,
    trace: Trace,
    tmp_path: Path,
    answers: dict[str, Any],
    decision: DecisionName,
) -> None:
    report = run(
        triage, [case("c", decision=decision)], trace, tmp_path, FakeJevRunner({"c": answers})
    )
    (result,) = report.results
    assert result.decision == decision
    assert result.passed
    assert result.to_json()["decision"] == decision


def test_the_graders_see_the_decision_but_not_risky(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def judge(expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        seen.append((expected.model_dump(mode="json", exclude_none=True), dict(actual)))
        return JudgeVerdict(passed=True, score=1.0, reason="ok")

    evaluate(
        triage,
        [case("c", risky=True)],
        trace,
        jev_runner=FakeJevRunner({}),
        jev_judge=judge,
        judge=judge,
        fixture_paths={"docs-only": tmp_path},
        workers=1,
    )
    ((expected, actual),) = seen
    assert actual["decision"] == "review"
    assert "risky" not in expected


def test_a_case_without_decision_gets_none(triage: JevSpec, trace: Trace, tmp_path: Path) -> None:
    report = run(triage, [case("c", decision=None)], trace, tmp_path, FakeJevRunner({}))
    assert report.results[0].decision is None
    assert report.skip_rate is None


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({"verdict": "ship"}, "no question 'verdict'"),
        ({"needs_review": "yes"}, "a noul question has no option"),
        ({"reviewers": "docs"}, "'docs' is not one of"),
    ],
)
def test_an_answer_the_agent_cannot_give_is_a_case_file_error(
    triage: JevSpec, trace: Trace, tmp_path: Path, answers: dict[str, str], message: str
) -> None:
    runner = FakeJevRunner({})
    with pytest.raises(CaseFileError, match=message):
        run(triage, [case("c", answers=answers)], trace, tmp_path, runner)
    assert runner.payloads == []


def test_a_decision_needs_a_jev_agent_with_gates(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    gateless = triage.model_copy(update={"gates": (), "routing": ()})
    with pytest.raises(CaseFileError, match="only a Jev agent with gates decides"):
        run(gateless, [case("c")], trace, tmp_path, FakeJevRunner({}))
    reviewer = resolve("code-reviewer", "^1.2")
    with pytest.raises(CaseFileError, match="only a Jev agent with gates decides"):
        run(reviewer, [case("c")], trace, tmp_path, FakeJevRunner({}))
    with pytest.raises(CaseFileError, match="only a Jev agent answers"):
        run(
            reviewer,
            [case("c", decision=None, answers={"x": "y"})],
            trace,
            tmp_path,
            FakeJevRunner({}),
        )


def test_risky_needs_a_decision() -> None:
    with pytest.raises(ValueError, match="risky: only meaningful with decision"):
        Expected(verdict=None, risky=True)


# --- metrics and the recall minimum


def test_the_metrics_count_cases_with_a_decision(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    cases = [
        case("docs", decision="skip"),
        case("tests", decision="skip"),
        case("sqli", risky=True),
        case("perf", risky=True),
        case("unsure", risky=True, decision="escalate"),
        case("plain", decision=None),
    ]
    answers = {"docs": SKIP, "tests": REVIEW, "sqli": REVIEW, "perf": SKIP, "unsure": UNSURE}
    report = run(triage, cases, trace, tmp_path, FakeJevRunner(answers))
    assert report.recall_risky == pytest.approx(2 / 3)
    assert report.skip_rate == pytest.approx(2 / 5)
    assert report.escalation_rate == pytest.approx(1 / 5)
    document = report.to_json()
    assert document["min_recall_risky"] == 0.98
    assert document["recall_risky"] == pytest.approx(2 / 3)
    assert not report.passed
    summary = report.table().splitlines()[-1]
    assert "recall_risky 67%, minimum 98%  skip_rate 40%  escalation_rate 20%: BELOW MINIMUM" in (
        summary
    )


def test_recall_below_the_minimum_fails_even_when_every_case_passes(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    cases = [case(f"r{i}", risky=True, decision="skip") for i in range(2)]
    report = run(triage, cases, trace, tmp_path, FakeJevRunner({"r0": SKIP, "r1": SKIP}))
    assert report.pass_rate == 1.0
    assert report.recall_risky == 0.0
    assert not report.passed


def test_full_recall_passes(triage: JevSpec, trace: Trace, tmp_path: Path) -> None:
    cases = [case("risky", risky=True), case("docs", decision="skip")]
    report = run(triage, cases, trace, tmp_path, FakeJevRunner({"docs": SKIP}))
    assert report.recall_risky == 1.0
    assert report.passed
    assert ": ok" in report.table().splitlines()[-1]


def test_a_failed_run_has_no_decision_and_is_not_counted(
    triage: JevSpec, trace: Trace, tmp_path: Path
) -> None:
    from runtime.jev_runner import JevError

    class Failing(FakeJevRunner):
        def __call__(
            self, spec: JevSpec, payload: Mapping[str, Any], trace: Trace, **kw: Any
        ) -> JevOutput:
            if payload["diff"] == "broken":
                raise JevError("HTTP 500")
            return super().__call__(spec, payload, trace, **kw)

    cases = [case("broken", risky=True), case("docs", decision="skip")]
    report = run(triage, cases, trace, tmp_path, Failing({"docs": SKIP}))
    assert report.results[0].error is not None
    assert report.results[0].decision is None
    assert report.recall_risky is None
    assert report.skip_rate == 1.0


def test_metrics_are_null_without_decisions() -> None:
    report = EvalReport("a", "1.0.0", "t", "e", 0.5, 0.0, 0, (), min_recall_risky=0.98)
    assert (report.recall_risky, report.skip_rate, report.escalation_rate) == (None, None, None)
    assert report.passed is False  # no cases: pass rate 0.0
    with_one = EvalReport(
        "a",
        "1.0.0",
        "t",
        "e",
        0.5,
        0.0,
        0,
        (CaseResult(case("c", decision=None), True, 0),),
        min_recall_risky=0.98,
    )
    assert with_one.passed
    assert "recall_risky" not in with_one.table()


# --- the diff-triage cases file


def test_the_diff_triage_cases_load_and_use_fixture_references(triage: JevSpec) -> None:
    cases = load_cases(triage.directory / CASES_FILE)
    assert len(cases) >= 6
    eval_run._check_expectations(triage, cases)
    for loaded in cases:
        assert loaded.grader == "jev"
        assert loaded.payload == {"files": {"$fixture": "files"}, "diff": {"$fixture": "diff"}}
        assert loaded.expected.verdict is None
        assert loaded.expected.decision is not None
    risky = [loaded for loaded in cases if loaded.expected.risky]
    assert len(risky) >= 4
    assert all(loaded.expected.decision == "review" for loaded in risky)
