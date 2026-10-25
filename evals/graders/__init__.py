"""Grade one agent answer against a case's expectations.

Deterministic checks run first and are cheap and exact: the verdict must match, unless the case
expects none, and every expected defect must be matched by some finding in the same file, on
overlapping lines, at or above the defect's `severity_floor`. A case that fails them is graded
failed on the spot and the judge is never called. Only an answer that passes them goes to the
grader the case names.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from evals.cases import SEVERITIES, Case, Expected, ExpectedDefect
from evals.graders.llm_judge import Judge, JudgeVerdict


@dataclass(frozen=True, slots=True)
class Deterministic:
    """The outcome of the deterministic checks: `failures` is empty exactly when `passed`."""

    passed: bool
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Grade:
    """The outcome of grading one answer. `judge` is None when the checks failed first."""

    passed: bool
    score: float
    reason: str
    deterministic: Deterministic
    judge: JudgeVerdict | None = None


def ensure_supported(grader: str) -> None:
    """Raise `NotImplementedError` for a grader that does not exist yet (`jev`, until #19)."""
    if grader == "jev":
        raise NotImplementedError("the jev grader arrives with #19; use `grader: llm_judge`")


def check_deterministic(expected: Expected, actual: Mapping[str, Any]) -> Deterministic:
    """Check `actual`, an agent's output as a dict, against `expected` without calling a model.

    Reads `verdict` and `findings` (each with `file`, `line_start`, `line_end`, `severity`) from
    the output; a judge's output has `merged_findings` instead, read the same way. A null expected
    verdict skips the verdict check. A finding with an unknown severity matches nothing.
    """
    failures: list[str] = []
    if expected.verdict is not None and actual.get("verdict") != expected.verdict:
        failures.append(f"verdict is {actual.get('verdict')!r}, expected {expected.verdict!r}")
    findings = actual.get("findings", actual.get("merged_findings")) or []
    failures.extend(
        problem
        for defect in expected.defects
        if (problem := _unmatched(defect, findings)) is not None
    )
    return Deterministic(passed=not failures, failures=tuple(failures))


def _unmatched(defect: ExpectedDefect, findings: list[Mapping[str, Any]]) -> str | None:
    """Return why no finding matches `defect`, or None when one does."""
    floor = SEVERITIES.index(defect.severity_floor)
    overlapping = [
        finding
        for finding in findings
        if finding.get("file") == defect.file
        and finding.get("line_start", 0) <= defect.line_end
        and finding.get("line_end", 0) >= defect.line_start
    ]
    if not overlapping:
        return (
            f"defect {defect.id}: no finding in {defect.file} "
            f"on lines {defect.line_start}-{defect.line_end}"
        )
    if all(_rank(finding) < floor for finding in overlapping):
        return (
            f"defect {defect.id}: found, but below severity {defect.severity_floor} "
            f"(highest is {max(overlapping, key=_rank).get('severity')})"
        )
    return None


def _rank(finding: Mapping[str, Any]) -> int:
    severity = finding.get("severity")
    return SEVERITIES.index(severity) if severity in SEVERITIES else -1


def grade(case: Case, actual: Mapping[str, Any], judge: Judge) -> Grade:
    """Grade `actual` against `case`: deterministic checks first, then the case's grader.

    A failed check returns a failed grade with score 0.0 and the failures as the reason, without
    calling `judge`. Raises `NotImplementedError` for the `jev` grader.
    """
    ensure_supported(case.grader)
    checks = check_deterministic(case.expected, actual)
    if not checks.passed:
        return Grade(False, 0.0, "; ".join(checks.failures), checks)
    verdict = judge(case.expected, actual)
    return Grade(verdict.passed, verdict.score, verdict.reason, checks, verdict)
