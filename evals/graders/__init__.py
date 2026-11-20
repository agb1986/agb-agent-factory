"""Grade one agent answer against a case's expectations.

Deterministic checks run first and are cheap and exact: the verdict must match, unless the case
expects none, so must a Jev agent's expected answers and gate decision, and every expected defect must be matched by some finding in the same file, on
overlapping lines, at or above the defect's `severity_floor`. A case that fails them is graded
failed on the spot and the judge is never called. Only an answer that passes them goes to the
grader the case names.

The `sandbox_tests` grader adds checks that run commands: see `evals.graders.sandbox_tests`. It
needs the repository the agent worked on, so `grade` takes its path. The `jev` grader is a Jev
noul question, `evals.graders.jev`; `grade` takes the judge for it apart from the LLM judge.
The `rubric` grader, `evals.graders.rubric`, adds the rubric's own deterministic checks (required
fields, forbidden strings, paths that must exist in the fixture repository) and then scores the
answer against its criteria; `grade` takes its judge apart too. A `sandbox_tests` case may also
carry `expected.rubric`, for an agent that writes code and prose about it: the rubric's checks run
after the commands, and its judge scores the prose instead of the LLM judge. A rubric case may also set
`expected.citations_resolve`: `evals.graders.citations` then fetches the pages the answer cites,
through the fetcher `grade` is given, and fails the case when a quote is not in its page. A rubric
case may set `expected.options_in_record` for an escalation summary: every option's evidence must
be found in the record the payload holds, by `runtime.escalation_summary.ungrounded`, the check
that also runs before a summary is stored.
The `incident` grader, `evals.graders.incident`, checks a triage-diagnostics answer: its suspected
paths must name the planted fault and its reproduction test must fail at the fixture's `head` with
the event's exception. It needs the repository path too, and calls no judge: an answer that
passes its checks passes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.cases import SEVERITIES, Case, Expected, ExpectedDefect
from evals.graders.citations import Fetcher, check_citations, web_fetcher
from evals.graders.incident import check_incident
from evals.graders.llm_judge import Judge, JudgeVerdict
from evals.graders.rubric import check_output
from evals.graders.sandbox_tests import check_run
from factory.exit import UsageError
from runtime.escalation_summary import ungrounded


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

    @property
    def grader_fallback(self) -> bool:
        """Whether the `jev` grader failed and another judge graded this answer instead."""
        return bool(getattr(self.judge, "grader_fallback", False))


def check_deterministic(expected: Expected, actual: Mapping[str, Any]) -> Deterministic:
    """Check `actual`, an agent's output as a dict, against `expected` without calling a model.

    Reads `verdict` and `findings` (each with `file`, `line_start`, `line_end`, `severity`) from
    the output; a judge's output has `merged_findings` instead, read the same way. A null expected
    verdict skips the verdict check. A finding with an unknown severity matches nothing. Each of
    `expected.answers` is compared with what that question picked, and `expected.decision` with
    the `decision` key the eval runner adds for a Jev agent, both the way the verdict is.
    """
    failures: list[str] = []
    if expected.verdict is not None and actual.get("verdict") != expected.verdict:
        failures.append(f"verdict is {actual.get('verdict')!r}, expected {expected.verdict!r}")
    for question_id, option in (expected.answers or {}).items():
        if actual.get(question_id) != option:
            failures.append(f"{question_id} is {actual.get(question_id)!r}, expected {option!r}")
    if expected.decision is not None and actual.get("decision") != expected.decision:
        failures.append(f"decision is {actual.get('decision')!r}, expected {expected.decision!r}")
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


def grade(
    case: Case,
    actual: Mapping[str, Any],
    judge: Judge,
    repo_path: str | Path | None = None,
    *,
    jev_judge: Judge | None = None,
    rubric_judge: Judge | None = None,
    fetcher: Fetcher | None = None,
) -> Grade:
    """Grade `actual` against `case`: deterministic checks first, then the case's grader.

    A failed check returns a failed grade with score 0.0 and the failures as the reason, without
    calling a judge. The `sandbox_tests` grader adds the checks of `expected.run`, which run
    commands in the work tree at `repo_path` and so need it. A case with the `jev` grader goes to
    `jev_judge`, a case with an `expected.rubric` (the `rubric` grader, or `sandbox_tests` with a
    rubric) to `rubric_judge` after the checks of the rubric, and every other case to `judge`.
    Raises `UsageError` for `sandbox_tests` without a `repo_path`, for `jev` without a `jev_judge`,
    for a rubric without a `rubric_judge`, and for a rubric with `paths_exist` without a
    `repo_path`. The `incident` grader runs the checks of `expected.incident` in a copy of
    `repo_path`, raises `UsageError` without it, and calls no judge: passing its checks passes.

    A case with `expected.citations_resolve` has it checked once the other checks have passed and
    before a judge is called; the pages come from `fetcher`, which defaults to `web.fetch`. A page
    that cannot be fetched or is outside the case's domains raises `CitationCheckError`, not a
    failed grade. Raises `UsageError` when the check is set and `fetcher` and `repo_path` are both
    None. A case with `expected.options_in_record` fails when `ungrounded(actual, case.payload)`
    finds an option whose evidence is not in the record, or a dollar figure the record lacks.
    """
    if case.grader == "jev" and jev_judge is None:
        raise UsageError("the jev grader needs a jev_judge to grade with")
    if case.expected.rubric is not None and rubric_judge is None:
        raise UsageError("a case with a rubric needs a rubric_judge to grade with")
    checks = check_deterministic(case.expected, actual)
    if case.grader == "sandbox_tests" and case.expected.run is not None:
        if repo_path is None:
            raise UsageError("the sandbox_tests grader needs the repository path to run tests in")
        ran = check_run(case.expected.run, actual, repo_path)
        checks = Deterministic(checks.passed and not ran, (*checks.failures, *ran))
    if case.grader == "incident" and case.expected.incident is not None:
        if repo_path is None:
            raise UsageError("the incident grader needs the repository path to run the test in")
        found = check_incident(case.expected.incident, actual, repo_path)
        checks = Deterministic(checks.passed and not found, (*checks.failures, *found))
    if case.expected.rubric is not None:
        found = check_output(case.expected.rubric, actual, repo_path)
        checks = Deterministic(checks.passed and not found, (*checks.failures, *found))
    if case.expected.options_in_record:
        found = ungrounded(actual, case.payload)
        checks = Deterministic(checks.passed and not found, (*checks.failures, *found))
    if checks.passed and case.expected.citations_resolve is not None:
        if fetcher is None and repo_path is None:
            raise UsageError("citations_resolve needs a fetcher or the repository path")
        fetch = fetcher if fetcher is not None else web_fetcher(str(repo_path))
        cited = check_citations(case.expected.citations_resolve, actual, fetch)
        checks = Deterministic(not cited, cited)
    if not checks.passed:
        return Grade(False, 0.0, "; ".join(checks.failures), checks)
    if case.grader == "incident":
        return Grade(True, 1.0, "the incident checks passed", checks)
    chosen = judge
    if case.grader == "jev" and jev_judge is not None:
        chosen = jev_judge
    elif case.expected.rubric is not None and rubric_judge is not None:
        chosen = rubric_judge
    verdict = chosen(case.expected, actual)
    return Grade(verdict.passed, verdict.score, verdict.reason, checks, verdict)
