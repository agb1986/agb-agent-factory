"""Tests for evals/graders/rubric.py and the `rubric` grader in `grade` and `evaluate`."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import anthropic
import pytest

from evals.cases import Case, Criterion, Expected, Rubric
from evals.graders import grade
from evals.graders.llm_judge import JudgeVerdict
from evals.graders.rubric import (
    RUBRIC_MODEL,
    RUBRIC_REF,
    SYSTEM_PROMPT,
    CriterionScore,
    RubricError,
    RubricGrader,
    check_output,
    score_rubric,
)
from evals.run import EvalReport, evaluate
from factory.exit import UsageError
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.budget import Budget
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace
from tests.evals.test_eval_run import FakeRunner
from tests.evals.test_llm_judge import FakeClient, _reply

RUBRIC = Rubric(
    criteria=(
        Criterion(
            id="names-the-bug", text="The summary names the off-by-one.", required=True, floor=0.5
        ),
        Criterion(id="says-where", text="The summary says which file is affected."),
        Criterion(id="is-short", text="The summary is two sentences or fewer."),
    ),
    threshold=0.7,
)
IDS = [criterion.id for criterion in RUBRIC.criteria]
ACTUAL: dict[str, Any] = {
    "verdict": "request_changes",
    "summary": "The rolling window in series/rolling.py is one element too long.",
    "findings": [],
}
REPLY_COST = cost_usd(RUBRIC_MODEL, TokenUsage(100, 20, 0, 0)) or 0.0


def _scores(*values: float) -> dict[str, CriterionScore]:
    return {
        i: CriterionScore(id=i, score=v, reason=f"{i} reason")
        for i, v in zip(IDS, values, strict=True)
    }


def _reply_json(*values: float, ids: list[str] | None = None) -> str:
    named = ids or IDS
    scores = [
        {"id": i, "score": v, "reason": "because"} for i, v in zip(named, values, strict=True)
    ]
    return json.dumps({"scores": scores})


def _case(rubric: Rubric = RUBRIC) -> Case:
    return Case(
        id="prose",
        fixture="off-by-one",
        payload={"base": "base", "head": "head"},
        expected=Expected(verdict=None, rubric=rubric),
        grader="rubric",
    )


@pytest.fixture
def trace(tmp_path: Path) -> Trace:
    return Trace(tmp_path / "traces.duckdb")


def _grader(trace: Trace, client: FakeClient, budget: Budget | None = None) -> RubricGrader:
    return RubricGrader(
        trace,
        tag="eval:x@1.0.0",
        pipeline_run_id="run-1",
        client=cast(anthropic.Anthropic, client),
        budget=budget,
    )


WITH_RUBRIC = Expected(verdict=None, rubric=RUBRIC)


# --- score_rubric, in code


def test_a_mean_at_the_threshold_passes_and_names_every_score() -> None:
    verdict = score_rubric(RUBRIC, _scores(0.8, 0.7, 0.6))
    assert verdict.passed
    assert verdict.score == pytest.approx(0.7)
    assert "names-the-bug 0.80" in verdict.reason
    assert [item.id for item in verdict.scores] == IDS


def test_a_mean_under_the_threshold_fails() -> None:
    verdict = score_rubric(RUBRIC, _scores(0.9, 0.5, 0.5))
    assert not verdict.passed
    assert "under the threshold 0.70" in verdict.reason


def test_a_required_criterion_under_its_floor_fails_whatever_the_mean() -> None:
    rubric = RUBRIC.model_copy(update={"threshold": 0.5})
    verdict = score_rubric(rubric, _scores(0.4, 1.0, 1.0))
    assert verdict.score == pytest.approx(0.8)
    assert not verdict.passed
    assert "required criterion names-the-bug scored 0.40, under its floor 0.50" in verdict.reason
    assert "names-the-bug reason" in verdict.reason


# --- check_output, without a model


def test_required_fields_must_be_present_and_not_empty() -> None:
    rubric = Rubric(
        criteria=RUBRIC.criteria,
        threshold=0.5,
        required_fields=("summary", "findings", "plan.steps", "verdict"),
    )
    failures = check_output(rubric, ACTUAL | {"verdict": "  "}, None)
    assert failures == (
        "required field findings is empty",
        "required field plan.steps is missing",
        "required field verdict is empty",
    )


def test_a_forbidden_string_is_found_in_nested_strings_case_sensitively() -> None:
    rubric = Rubric(criteria=RUBRIC.criteria, threshold=0.5, forbidden=("TODO", "rolling"))
    actual = ACTUAL | {"findings": [{"body": "TODO later"}], "summary": "Rolling only."}
    assert check_output(rubric, actual, None) == ("forbidden string 'TODO' in findings.0.body",)


def test_paths_must_exist_inside_the_fixture_repository(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("a")
    rubric = Rubric(criteria=RUBRIC.criteria, threshold=0.5, paths_exist=("files", "one", "gone"))
    actual = {"files": ["docs/a.md", "docs/b.md", "../outside"], "one": "docs"}
    assert check_output(rubric, actual, tmp_path) == (
        "files: 'docs/b.md' does not exist in the fixture repository",
        "files: '../outside' is outside the fixture repository",
        "path field gone is missing",
    )


def test_paths_exist_without_a_repository_is_a_usage_error() -> None:
    rubric = Rubric(criteria=RUBRIC.criteria, threshold=0.5, paths_exist=("files",))
    with pytest.raises(UsageError):
        check_output(rubric, {"files": []}, None)


# --- the grader against a fake client


def test_the_request_sends_only_the_criteria_and_the_answer(trace: Trace) -> None:
    client = FakeClient(_reply(_reply_json(1.0, 1.0, 1.0)))
    _grader(trace, client)(WITH_RUBRIC, ACTUAL)
    request = client.requests[0]
    assert request["model"] == RUBRIC_MODEL == "claude-haiku-5-5"
    assert request["system"] == SYSTEM_PROMPT
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k"} & set(request)
    sent = json.loads(request["messages"][0]["content"])
    assert sent == {
        "criteria": [{"id": c.id, "text": c.text} for c in RUBRIC.criteria],
        "answer": ACTUAL,
    }


def test_a_call_is_traced_with_its_cost_under_the_run_id(trace: Trace) -> None:
    budget = Budget(1.0)
    client = FakeClient(_reply(_reply_json(1.0, 0.5, 0.5)))
    verdict = _grader(trace, client, budget)(WITH_RUBRIC, ACTUAL)
    assert verdict.score == pytest.approx(2 / 3)
    rows = (
        trace._connect()
        .execute("SELECT agent_ref, tag, pipeline_run_id, cost_usd, error, output_json FROM traces")
        .fetchall()
    )
    [(ref, tag, run_id, cost, error, output)] = rows
    assert (ref, tag, run_id, error) == (RUBRIC_REF, "eval:x@1.0.0", "run-1", None)
    assert REPLY_COST > 0
    assert cost == pytest.approx(REPLY_COST)
    assert budget.spent == pytest.approx(REPLY_COST)
    assert [item["id"] for item in json.loads(output)["scores"]] == IDS


def test_scores_for_the_wrong_criteria_are_retried_once(trace: Trace) -> None:
    wrong = _reply_json(1.0, 1.0, ids=["names-the-bug", "made-up"])
    client = FakeClient(_reply(wrong), _reply(_reply_json(1.0, 1.0, 1.0)))
    verdict = _grader(trace, client)(WITH_RUBRIC, ACTUAL)
    assert verdict.passed
    retry = client.requests[1]["messages"]
    assert [message["role"] for message in retry] == ["user", "assistant", "user"]
    assert "no score for says-where, is-short" in retry[2]["content"]
    assert "unknown criteria made-up" in retry[2]["content"]


def test_two_bad_answers_raise_a_rubric_error_and_trace_it(trace: Trace) -> None:
    client = FakeClient(_reply("nope"), _reply(_reply_json(2.0, 1.0, 1.0)))
    with pytest.raises(RubricError, match="invalid scores twice"):
        _grader(trace, client)(WITH_RUBRIC, ACTUAL)
    [(error,)] = trace._connect().execute("SELECT error FROM traces").fetchall()
    assert "RubricError" in error


def test_the_grader_refuses_a_case_without_a_rubric(trace: Trace) -> None:
    with pytest.raises(UsageError):
        _grader(trace, FakeClient())(Expected(verdict=None), ACTUAL)


# --- grade


class FakeRubricJudge:
    """A mocked rubric grader: scores the criteria with `values` in order, or raises `error`."""

    def __init__(self, *values: float, error: Exception | None = None) -> None:
        self.values = values
        self.error = error
        self.calls = 0

    def __call__(self, expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert expected.rubric is not None
        return score_rubric(expected.rubric, _scores(*self.values))


def test_grade_needs_a_rubric_judge_for_a_rubric_case() -> None:
    with pytest.raises(UsageError, match="rubric_judge"):
        grade(_case(), ACTUAL, FakeRubricJudge(1, 1, 1))


def test_a_failed_deterministic_check_skips_the_grader() -> None:
    rubric = RUBRIC.model_copy(update={"forbidden": ("rolling",)})
    judge = FakeRubricJudge(1, 1, 1)
    graded = grade(_case(rubric), ACTUAL, FakeRubricJudge(), rubric_judge=judge)
    assert not graded.passed
    assert graded.judge is None
    assert judge.calls == 0
    assert "forbidden string 'rolling' in summary" in graded.reason


# --- evaluate, with the agent and the grader mocked


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("code-reviewer", "^1.2")
    assert isinstance(found, LLMSpec)
    return found


def _run(
    spec: LLMSpec, trace: Trace, tmp_path: Path, rubric_judge: Any, rubric: Rubric = RUBRIC
) -> EvalReport:
    repo = tmp_path / "repos" / "off-by-one"
    repo.mkdir(parents=True)
    answer = {key: value for key, value in ACTUAL.items() if key != "summary"}
    return evaluate(
        spec,
        [_case(rubric)],
        trace,
        runner=FakeRunner(spec, {"off-by-one": answer}),
        rubric_judge=rubric_judge,
        fixture_paths={"off-by-one": repo},
        eval_run_id="eval-run-1",
    )


def test_a_rubric_case_passes_on_the_mean(spec: LLMSpec, trace: Trace, tmp_path: Path) -> None:
    [result] = _run(spec, trace, tmp_path, FakeRubricJudge(0.9, 0.8, 0.7)).results
    assert result.passed
    assert result.error is None
    assert result.grade is not None
    assert result.grade.score == pytest.approx(0.8)


def test_a_rubric_case_fails_on_the_mean(spec: LLMSpec, trace: Trace, tmp_path: Path) -> None:
    [result] = _run(spec, trace, tmp_path, FakeRubricJudge(0.6, 0.6, 0.6)).results
    assert not result.passed
    assert result.error is None
    assert result.grade is not None
    assert "under the threshold" in result.grade.reason


def test_a_rubric_case_fails_on_a_required_criterion(
    spec: LLMSpec, trace: Trace, tmp_path: Path
) -> None:
    rubric = RUBRIC.model_copy(update={"threshold": 0.5})
    [result] = _run(spec, trace, tmp_path, FakeRubricJudge(0.2, 1.0, 1.0), rubric).results
    assert not result.passed
    assert result.grade is not None
    assert result.grade.score == pytest.approx(2.2 / 3)
    assert "required criterion names-the-bug" in result.grade.reason


def test_a_grader_error_is_a_case_error_not_a_pass(
    spec: LLMSpec, trace: Trace, tmp_path: Path
) -> None:
    judge = FakeRubricJudge(error=RubricError("broken"))
    [result] = _run(spec, trace, tmp_path, judge).results
    assert not result.passed
    assert result.error == "RubricError: broken"


def test_the_default_grader_is_traced_under_the_eval_run_id(
    spec: LLMSpec, trace: Trace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(_reply(_reply_json(1.0, 1.0, 1.0)))
    monkeypatch.setattr(RubricGrader, "_get_client", lambda self: client)
    report = _run(spec, trace, tmp_path, None)
    [result] = report.results
    assert result.passed
    rows = trace._connect().execute(
        "SELECT agent_ref, tag, pipeline_run_id FROM traces WHERE agent_ref = ?", [RUBRIC_REF]
    )
    assert rows.fetchall() == [(RUBRIC_REF, f"eval:{spec.address}", "eval-run-1")]
    assert report.cost_usd == pytest.approx(REPLY_COST)
