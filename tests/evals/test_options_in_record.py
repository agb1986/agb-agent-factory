"""Tests for the `options_in_record` check of escalation-summary eval cases."""

from typing import Any

import pytest
from pydantic import ValidationError

from evals.cases import Case, Criterion, Expected, Rubric
from evals.graders import grade
from evals.graders.llm_judge import JudgeVerdict

RECORD: dict[str, Any] = {
    "id": "esc-1",
    "work_item_id": "wi-1",
    "trigger": "blocked_output",
    "question": "Which module owns the parser?",
    "from_status": "in_progress",
    "cost_usd": 0.5,
    "diff": "",
    "agent_outputs": [],
    "suite_tail": "",
}


def _summary(evidence: str) -> dict[str, Any]:
    option = {
        "label": "Answer",
        "takes": "another_run",
        "cost": "Another run.",
        "evidence": evidence,
    }
    return {"paragraph": "wi-1 is blocked.", "options": [option]}


class PassingJudge:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, expected: Expected, actual: Any) -> JudgeVerdict:
        self.calls += 1
        return JudgeVerdict(passed=True, score=1.0, reason="fine")


def _case(*, options_in_record: bool = True) -> Case:
    rubric = Rubric(criteria=(Criterion(id="ok", text="fine"),), threshold=0.5)
    return Case(
        id="summary",
        fixture="storefront",
        payload=RECORD,
        expected=Expected(verdict=None, rubric=rubric, options_in_record=options_in_record),
        grader="rubric",
    )


def test_only_a_rubric_case_may_use_the_check() -> None:
    with pytest.raises(ValidationError, match="only a rubric case"):
        Case(
            id="c",
            fixture="f",
            expected=Expected(verdict="approve", options_in_record=True),
            grader="llm_judge",
        )


def test_an_option_not_in_the_record_fails_before_the_judge_is_called() -> None:
    judge = PassingJudge()
    graded = grade(_case(), _summary("Which team owns the lexer?"), judge, rubric_judge=judge)
    assert not graded.passed
    assert "evidence is not in the record" in graded.reason
    assert judge.calls == 0


def test_an_invented_dollar_figure_fails_the_case() -> None:
    judge = PassingJudge()
    summary = _summary("Which module owns the parser?")
    summary["options"][0]["cost"] = "About $3.00 more."
    graded = grade(_case(), summary, judge, rubric_judge=judge)
    assert not graded.passed
    assert judge.calls == 0


def test_a_grounded_summary_goes_on_to_the_judge() -> None:
    judge = PassingJudge()
    graded = grade(_case(), _summary("Which module owns the parser?"), judge, rubric_judge=judge)
    assert graded.passed
    assert judge.calls == 1


def test_without_the_check_an_ungrounded_option_is_left_to_the_judge() -> None:
    judge = PassingJudge()
    case = _case(options_in_record=False)
    graded = grade(case, _summary("Which team owns the lexer?"), judge, rubric_judge=judge)
    assert graded.passed
    assert judge.calls == 1
