"""Tests for evals/graders: the deterministic checks and the short-circuit before the judge."""

from collections.abc import Mapping
from typing import Any

import pytest

from evals.cases import Case, Expected
from evals.graders import check_deterministic, grade
from evals.graders.jev import JevVerdict
from evals.graders.llm_judge import JudgeVerdict
from factory.exit import UsageError

EXPECTED = Expected.model_validate(
    {
        "verdict": "request_changes",
        "defects": [
            {
                "id": "boundary",
                "file": "shop/shipping.py",
                "line_start": 12,
                "line_end": 14,
                "severity_floor": "major",
            }
        ],
    }
)


def _finding(**overrides: Any) -> dict[str, Any]:
    finding = {"file": "shop/shipping.py", "line_start": 12, "line_end": 12, "severity": "major"}
    return finding | overrides


def _answer(*findings: dict[str, Any], verdict: str = "request_changes") -> dict[str, Any]:
    return {"verdict": verdict, "findings": list(findings), "summary": "s"}


def _case(expected: Expected = EXPECTED, grader: str = "llm_judge") -> Case:
    return Case.model_validate(
        {
            "id": "c",
            "fixture": "f",
            "payload": {},
            "expected": expected.model_dump(),
            "grader": grader,
        }
    )


def _judge(passed: bool = True, score: float = 0.9) -> JudgeVerdict:
    return JudgeVerdict(passed=passed, score=score, reason="judged")


def _no_judge(expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
    raise AssertionError("the judge must not be called")


def test_a_matching_finding_and_verdict_pass() -> None:
    assert check_deterministic(EXPECTED, _answer(_finding())).passed


@pytest.mark.parametrize(
    "finding",
    [
        _finding(line_start=14, line_end=20),  # overlaps the last line
        _finding(line_start=1, line_end=12),  # overlaps the first line
        _finding(severity="blocker"),  # above the floor
    ],
)
def test_overlapping_lines_and_a_severity_at_or_above_the_floor_match(
    finding: dict[str, Any],
) -> None:
    assert check_deterministic(EXPECTED, _answer(finding)).passed


@pytest.mark.parametrize(
    ("finding", "fragment"),
    [
        (_finding(file="shop/other.py"), "no finding in shop/shipping.py"),
        (_finding(line_start=15, line_end=16), "on lines 12-14"),
        (_finding(line_start=1, line_end=11), "on lines 12-14"),
        (_finding(severity="minor"), "below severity major"),
        (_finding(severity="nonsense"), "below severity major"),
    ],
)
def test_a_finding_that_does_not_match_fails_with_the_reason(
    finding: dict[str, Any], fragment: str
) -> None:
    checks = check_deterministic(EXPECTED, _answer(finding))
    assert not checks.passed
    assert fragment in checks.failures[0]


def test_a_wrong_verdict_fails() -> None:
    checks = check_deterministic(EXPECTED, _answer(_finding(), verdict="approve"))
    assert checks.failures == ("verdict is 'approve', expected 'request_changes'",)


def test_every_failure_is_reported() -> None:
    checks = check_deterministic(EXPECTED, _answer(verdict="approve"))
    assert len(checks.failures) == 2


def test_a_clean_case_expects_no_findings_to_match() -> None:
    clean = Expected(verdict="approve")
    assert check_deterministic(clean, _answer(verdict="approve")).passed


def test_a_null_verdict_is_not_checked() -> None:
    unchecked = EXPECTED.model_copy(update={"verdict": None})
    assert check_deterministic(unchecked, _answer(_finding(), verdict="approve")).passed
    assert check_deterministic(unchecked, {"findings": [_finding()]}).passed


def test_a_judges_merged_findings_are_matched_like_findings() -> None:
    unchecked = EXPECTED.model_copy(update={"verdict": None})
    judgement = {"scores": {}, "merged_findings": [_finding()], "rationale": "r"}
    assert check_deterministic(unchecked, judgement).passed
    weak = judgement | {"merged_findings": [_finding(severity="minor")]}
    checks = check_deterministic(unchecked, weak)
    assert not checks.passed
    assert "below severity major" in checks.failures[0]


def test_a_judgement_that_dropped_the_defect_fails() -> None:
    unchecked = EXPECTED.model_copy(update={"verdict": None})
    judgement = {"scores": {}, "merged_findings": [], "rationale": "r"}
    checks = check_deterministic(unchecked, judgement)
    assert checks.failures and "no finding in shop/shipping.py" in checks.failures[0]


def test_a_deterministic_failure_never_calls_the_judge() -> None:
    result = grade(_case(), _answer(verdict="approve"), _no_judge)
    assert not result.passed
    assert result.score == 0.0
    assert result.judge is None
    assert "verdict is 'approve'" in result.reason


def test_a_deterministic_pass_goes_to_the_judge_and_takes_its_verdict() -> None:
    seen: list[tuple[Expected, Mapping[str, Any]]] = []

    def judge(expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        seen.append((expected, actual))
        return _judge(passed=False, score=0.4)

    answer = _answer(_finding())
    result = grade(_case(), answer, judge)
    assert seen == [(EXPECTED, answer)]
    assert (result.passed, result.score, result.reason) == (False, 0.4, "judged")
    assert result.deterministic.passed


def test_the_jev_grader_needs_a_jev_judge() -> None:
    with pytest.raises(UsageError, match="jev_judge"):
        grade(_case(grader="jev"), _answer(_finding()), _no_judge)


def test_a_jev_case_goes_to_the_jev_judge_and_not_the_llm_judge() -> None:
    jev = JevVerdict(passed=True, score=0.9, reason="matches", grader_fallback=True)
    result = grade(_case(grader="jev"), _answer(_finding()), _no_judge, jev_judge=lambda *_: jev)
    assert (result.passed, result.score, result.reason) == (True, 0.9, "matches")
    assert result.grader_fallback is True


def test_an_llm_judge_case_ignores_the_jev_judge() -> None:
    llm = JudgeVerdict(passed=True, score=0.5, reason="llm")
    result = grade(_case(), _answer(_finding()), lambda *_: llm, jev_judge=_no_judge)
    assert (result.passed, result.grader_fallback) == (True, False)
