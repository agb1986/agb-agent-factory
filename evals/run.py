"""Run an agent's eval cases and gate on its `evals.min_pass_rate`.

`evaluate` builds each case's fixture, runs the agent on it through `runtime.runner.run` with the
trace tag `eval:<name>@<version>`, grades the answer (`evals.graders`), and returns an
`EvalReport`. Cases run on a thread pool; results keep the order of the cases file. `command` is
what `factory eval <name>[@^M.m]` runs, and `python -m evals.run` is the same command.

A Jev agent (`kind: jev`) is run through `run_jev` with the same tag, and its answers are flattened
to `{question_id: picked}` (the noul `p`, the choice `answer`, the score `level` or `value`) for the
graders; a case graded `jev` goes to the Jev grader, with the LLM judge as its fallback.

A payload value `{"$fixture": "diff"}` or `{"$fixture": "files"}` is replaced by the fixture's
base-to-head diff or the files it changes, read the way the review panel reads them. For a Jev agent
with gates, a case that expects a `decision` has `runtime.gates.decide` run on the answers, and the
report counts those decisions: `recall_risky`, `skip_rate` and `escalation_rate`. A manifest's
`evals.min_recall_risky` is a second minimum the eval must meet.

A case whose agent run fails with a typed error (a refusal, a timeout, invalid output) is a failed
case with that error, not a failed eval. A missing or rejected API key, and a payload the agent
cannot accept, end the whole eval, since every other case would fail the same way.

An agent with `sandbox: true` changes the repository it works on. Its cases share one built
fixture, so each case runs on its own copy, and the `sandbox_tests` grader runs the tests in that
copy after the agent is done.
"""

import re
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import Version
from pydantic import BaseModel

from evals.cases import CASES_FILE, FIXTURE_REF, Case, CaseFileError, load_cases
from evals.graders import Deterministic, Grade, grade
from evals.graders.citations import Fetcher
from evals.graders.jev import JevJudge
from evals.graders.llm_judge import Judge, JudgeVerdict, LLMJudge
from evals.graders.rubric import RubricGrader
from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from fixtures.build import build
from pipelines.review_panel import changed_files
from registry.jev_spec import JevOutput, JevSpec, NoulQuestion
from registry.resolver import AgentNotFound, resolve
from registry.resolver import list_agents as _list_agents
from registry.spec import LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget, BudgetExceeded
from runtime.gates import Decision, decide
from runtime.jev_runner import run_jev
from runtime.runner import run
from runtime.trace import Trace, TraceRow
from tools import MAX_OUTPUT_BYTES
from tools.git import show_diff

DEFAULT_WORKERS = 4

type Runner = Callable[..., BaseModel]
"""Anything called like `runtime.runner.run`; tests inject a fake."""

type JevRunner = Callable[..., JevOutput]
"""Anything called like `runtime.jev_runner.run_jev`; tests inject a fake."""

FIXTURE_BASE, FIXTURE_HEAD = "base", "head"
"""The branches of a built fixture whose diff a `$fixture` reference reads."""

_CUT_MARKER = re.compile(r"^\[truncated: \d+ bytes dropped, limit \d+ bytes\]$")
"""The last line `tools._common.truncate` adds to a diff it cut, as the review panel matches it."""


class _SharedTrace(Trace):
    """A view of a `Trace` whose writes are safe to make from the eval's worker threads.

    A plain `Trace` serialises its own calls, but the trace a caller passes in may be a subclass
    that does not, so every write goes through the wrapped trace under the view's lock. The view
    itself never opens a connection.
    """

    def __init__(self, inner: Trace) -> None:
        super().__init__(inner.path)
        self._inner = inner

    def record(self, row: TraceRow) -> None:
        """Write one run to the wrapped trace, one thread at a time."""
        with self._lock:
            self._inner.record(row)


class _CaseTrace(Trace):
    """A view of the eval's trace for one case, which notes whether a Jev run cut its input.

    `run_jev` says so only in the `truncated` key of the `decision_json` of the row it writes, so
    the view forwards each row and reads that key, as the review panel does.
    """

    def __init__(self, inner: Trace) -> None:
        super().__init__(inner.path)
        self._inner = inner
        self.truncated = False

    def record(self, row: TraceRow) -> None:
        """Write one run to the wrapped trace and note a cut input."""
        self._inner.record(row)
        if isinstance(row.decision_json, dict) and row.decision_json.get("truncated"):
            self.truncated = True


@dataclass(frozen=True, slots=True)
class CaseResult:
    """How one case went. `error` is set, and `grade` is None, when the agent run failed.

    `decision` is `skip`, `review` or `escalate` when the case expects a gate decision and the
    agent answered, else None.
    """

    case: Case
    passed: bool
    duration_ms: int
    grade: Grade | None = None
    error: str | None = None
    decision: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the case as the JSON document describes it."""
        grade = self.grade
        judge = None if grade is None else grade.judge
        checks = grade.deterministic if grade is not None else Deterministic(False, ())
        return {
            "id": self.case.id,
            "fixture": self.case.fixture,
            "grader": self.case.grader,
            "passed": self.passed,
            "score": 0.0 if grade is None else grade.score,
            "reason": self.error if grade is None else grade.reason,
            "deterministic": {"passed": checks.passed, "failures": list(checks.failures)},
            "judge": None if judge is None else _judge_json(judge),
            "grader_fallback": grade is not None and grade.grader_fallback,
            "decision": self.decision,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    """The result of one eval: every case, and the numbers the exit code is decided on."""

    agent: str
    version: str
    tag: str
    eval_run_id: str
    min_pass_rate: float
    cost_usd: float
    duration_ms: int
    results: tuple[CaseResult, ...]
    min_recall_risky: float | None = None

    @property
    def total(self) -> int:
        """The number of cases run."""
        return len(self.results)

    @property
    def passed_count(self) -> int:
        """The number of cases that passed."""
        return sum(result.passed for result in self.results)

    @property
    def pass_rate(self) -> float:
        """The fraction of cases that passed, 0.0 when there are none."""
        return self.passed_count / self.total if self.total else 0.0

    @property
    def recall_risky(self) -> float | None:
        """The share of risky cases with a decision that was not `skip`; None without any."""
        risky = [result for result in self._decided if result.case.expected.risky]
        return _share(risky, lambda result: result.decision != "skip")

    @property
    def skip_rate(self) -> float | None:
        """The share of cases with a decision that was `skip`; None without any."""
        return _share(self._decided, lambda result: result.decision == "skip")

    @property
    def escalation_rate(self) -> float | None:
        """The share of cases with a decision that was `escalate`; None without any."""
        return _share(self._decided, lambda result: result.decision == "escalate")

    @property
    def _decided(self) -> list[CaseResult]:
        return [result for result in self.results if result.decision is not None]

    @property
    def passed(self) -> bool:
        """Whether the pass rate meets `evals.min_pass_rate`, and recall `evals.min_recall_risky`.

        The recall minimum applies only when the manifest sets one and some risky case has a
        decision.
        """
        recall, minimum = self.recall_risky, self.min_recall_risky
        recall_ok = recall is None or minimum is None or recall >= minimum
        return self.pass_rate >= self.min_pass_rate and recall_ok

    def to_json(self) -> dict[str, Any]:
        """Return the document `factory eval --json` prints. See "Eval cases" in CONVENTIONS."""
        return {
            "agent": self.agent,
            "version": self.version,
            "ref": f"{self.agent}@{self.version}",
            "tag": self.tag,
            "eval_run_id": self.eval_run_id,
            "min_pass_rate": self.min_pass_rate,
            "pass_rate": self.pass_rate,
            "min_recall_risky": self.min_recall_risky,
            "recall_risky": self.recall_risky,
            "skip_rate": self.skip_rate,
            "escalation_rate": self.escalation_rate,
            "passed": self.passed,
            "total": self.total,
            "passed_count": self.passed_count,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "cases": [result.to_json() for result in self.results],
        }

    def table(self) -> str:
        """Return the report as plain text lines for a terminal."""
        lines = [f"{self.agent}@{self.version}  tag {self.tag}"]
        width = max((len(result.case.id) for result in self.results), default=0)
        for result in self.results:
            score = "  -- " if result.grade is None else f"{result.grade.score:5.2f}"
            reason = result.error if result.grade is None else result.grade.reason
            lines.append(
                f"  {'PASS' if result.passed else 'FAIL'}  {result.case.id:<{width}}  {score}  "
                f"{' '.join((reason or '').split())}"
            )
        verdict = "ok" if self.passed else "BELOW MINIMUM"
        lines.append(
            f"{self.passed_count}/{self.total} passed ({self.pass_rate:.0%}), minimum "
            f"{self.min_pass_rate:.0%}{self._metrics_text()}: {verdict}  "
            f"cost ${self.cost_usd:.4f}  {self.duration_ms / 1000:.1f}s"
        )
        return "\n".join(lines)

    def _metrics_text(self) -> str:
        """The decision metrics for the summary line; empty when no case has a decision."""
        if not self._decided:
            return ""
        recall = _percent(self.recall_risky)
        if self.min_recall_risky is not None:
            recall += f", minimum {self.min_recall_risky:.0%}"
        return (
            f"  recall_risky {recall}  skip_rate {_percent(self.skip_rate)}  "
            f"escalation_rate {_percent(self.escalation_rate)}"
        )


def _share(results: Sequence[CaseResult], counts: Callable[[CaseResult], bool]) -> float | None:
    return sum(map(counts, results)) / len(results) if results else None


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def evaluate(
    spec: LLMSpec | JevSpec,
    cases: Sequence[Case],
    trace: Trace,
    *,
    runner: Runner = run,
    judge: Judge | None = None,
    jev_runner: JevRunner = run_jev,
    jev_judge: Judge | None = None,
    workers: int = DEFAULT_WORKERS,
    eval_run_id: str | None = None,
    fixture_paths: Mapping[str, Path] | None = None,
    budget: Budget | None = None,
    rubric_judge: Judge | None = None,
    fetcher: Fetcher | None = None,
) -> EvalReport:
    """Run `cases` against `spec` and return the report.

    `runner` is called as `runtime.runner.run` is, with `tag` set to `eval:<address>` and the eval's
    id as `pipeline_run_id`; `judge` grades the answers that pass the deterministic checks and
    defaults to an `LLMJudge` on `trace`. A Jev agent is run by `jev_runner`, called as
    `runtime.jev_runner.run_jev` is, and its answer reaches the checks as a dict with each
    question id mapped to what it picked: `p` for a noul question, `answer` for a choice, `level`
    for a score. Cases with `grader: jev` are graded by `jev_judge`, which defaults to a `JevJudge`
    that falls back to `judge`. `fixture_paths` maps fixture names to built repositories;
    by default every fixture the cases use is built into a temporary directory first, so a missing
    one is a `FixtureNotFound` before any model is called. The input model's `repo_path` field, when
    it has one, is set to the fixture's path, and each `$fixture` reference in a payload is replaced
    by the fixture's diff or changed files. A case that expects a `decision` has `decide` run on
    the Jev answers with the input counted as truncated when the diff or the Jev runner was cut,
    and the decision reaches the graders as the `decision` key. Every `runner` and `jev_runner` call is charged to
    `budget` (default: a new `Budget(DEFAULT_BUDGET_USD)`), and so are the calls of the default
    `LLMJudge` and `JevJudge`; a `judge` or `jev_judge` passed in is charged only if it was built
    with the budget. A case that runs out of budget is not a failed case: the evaluation ends with
    `BudgetExceeded`. Cases with `grader: rubric` are graded by `rubric_judge`, which defaults to a
    `RubricGrader` on `trace` with the same tag, run id and budget, built only when a case needs it.
    A case with `expected.citations_resolve` has the pages it cites read through `fetcher`, which
    defaults to `web.fetch`; one that cannot be read is a case error.

    Raises `UsageError` for `workers` below 1, `CaseFileError` for `answers` or `decision` the
    agent cannot give, `AuthOrConfigError` for a missing or rejected key, `BudgetExceeded`, and
    `FixtureNotFound` or `BuildError` from the fixture build.
    """
    if workers < 1:
        raise UsageError(f"--workers must be at least 1, not {workers}")
    _check_expectations(spec, cases)
    eval_run_id = eval_run_id or str(uuid.uuid4())
    budget = budget or Budget(DEFAULT_BUDGET_USD)
    tag = f"eval:{spec.address}"
    start = time.monotonic()
    shared = _SharedTrace(trace)
    own_judge = judge is None
    llm_judge = (
        LLMJudge(shared, tag=tag, pipeline_run_id=eval_run_id, budget=budget) if own_judge else None
    )
    own_rubric = rubric_judge is None and any(case.grader == "rubric" for case in cases)
    rubric_grader = (
        RubricGrader(shared, tag=tag, pipeline_run_id=eval_run_id, budget=budget)
        if own_rubric
        else None
    )
    rubric_judge = rubric_judge if rubric_judge is not None else rubric_grader
    try:
        with tempfile.TemporaryDirectory(prefix="factory-eval-") as scratch:
            paths = fixture_paths or _build_fixtures(cases, Path(scratch))
            llm = judge if judge is not None else _required(llm_judge)
            if jev_judge is None:
                jev_judge = JevJudge(
                    shared, llm, tag=tag, pipeline_run_id=eval_run_id, budget=budget
                )
            results = _run_all(
                spec,
                cases,
                paths,
                shared,
                runner,
                jev_runner,
                llm,
                jev_judge,
                workers,
                eval_run_id,
                tag,
                budget,
                rubric_judge,
                fetcher,
            )
    finally:
        if llm_judge is not None:
            llm_judge.close()
        if rubric_grader is not None:
            rubric_grader.close()
    return EvalReport(
        agent=spec.name,
        version=spec.version,
        tag=tag,
        eval_run_id=eval_run_id,
        min_pass_rate=spec.evals.min_pass_rate,
        cost_usd=trace.cost_by_pipeline_run(eval_run_id),
        duration_ms=int((time.monotonic() - start) * 1000),
        results=tuple(results),
        min_recall_risky=spec.evals.min_recall_risky if isinstance(spec, JevSpec) else None,
    )


def _check_expectations(spec: LLMSpec | JevSpec, cases: Sequence[Case]) -> None:
    """Raise `CaseFileError` for an expected answer or decision the agent `spec` cannot give."""
    questions = {q.id: q for q in spec.questions} if isinstance(spec, JevSpec) else {}
    for case in cases:
        expected = case.expected
        where = f"{spec.name}: case {case.id!r}: expected"
        if expected.answers and not isinstance(spec, JevSpec):
            raise CaseFileError(f"{where}.answers: only a Jev agent answers questions")
        for question_id, option in (expected.answers or {}).items():
            question = questions.get(question_id)
            if question is None:
                raise CaseFileError(f"{where}.answers: no question {question_id!r}")
            if isinstance(question, NoulQuestion):
                raise CaseFileError(
                    f"{where}.answers.{question_id}: a noul question has no option to expect"
                )
            allowed = question.options if question.type == "choice" else question.levels
            if option not in allowed:
                raise CaseFileError(
                    f"{where}.answers.{question_id}: {option!r} is not one of {list(allowed)}"
                )
        if expected.decision is None:
            continue
        if not isinstance(spec, JevSpec) or not spec.gates:
            raise CaseFileError(f"{where}.decision: only a Jev agent with gates decides")
        if "decision" in questions:
            raise CaseFileError(f"{where}.decision: the agent has a question named decision")


def _required(judge: LLMJudge | None) -> LLMJudge:
    assert judge is not None
    return judge


def _build_fixtures(cases: Sequence[Case], dest: Path) -> dict[str, Path]:
    names = sorted({case.fixture for case in cases})
    return {built.name: built.path for built in build(names, dest=dest)}


def _run_all(
    spec: LLMSpec | JevSpec,
    cases: Sequence[Case],
    paths: Mapping[str, Path],
    trace: Trace,
    runner: Runner,
    jev_runner: JevRunner,
    judge: Judge,
    jev_judge: Judge,
    workers: int,
    eval_run_id: str,
    tag: str,
    budget: Budget,
    rubric_judge: Judge | None = None,
    fetcher: Fetcher | None = None,
) -> list[CaseResult]:
    def one(case: Case) -> CaseResult:
        return _run_case(
            spec,
            case,
            paths[case.fixture],
            trace,
            runner,
            jev_runner,
            judge,
            jev_judge,
            eval_run_id,
            tag,
            budget,
            rubric_judge,
            fetcher,
        )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval") as pool:
        futures = [pool.submit(one, case) for case in cases]
        try:
            return [future.result() for future in futures]
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise


def _run_case(
    spec: LLMSpec | JevSpec,
    case: Case,
    repo_path: Path,
    trace: Trace,
    runner: Runner,
    jev_runner: JevRunner,
    judge: Judge,
    jev_judge: Judge,
    eval_run_id: str,
    tag: str,
    budget: Budget,
    rubric_judge: Judge | None = None,
    fetcher: Fetcher | None = None,
) -> CaseResult:
    start = time.monotonic()
    decision: str | None = None
    with tempfile.TemporaryDirectory(prefix="factory-eval-case-") as scratch:
        try:
            payload, cut = _fill_fixture_refs(case.payload, repo_path)
            if isinstance(spec, JevSpec):
                capture = _CaseTrace(trace)
                answers = jev_runner(
                    spec, payload, capture, pipeline_run_id=eval_run_id, budget=budget, tag=tag
                )
                actual = _jev_answers(answers)
                if case.expected.decision is not None:
                    decision = _decision_name(decide(spec, answers, cut or capture.truncated))
                    actual["decision"] = decision
            else:
                if spec.sandbox:
                    repo_path = _private_copy(repo_path, Path(scratch))
                if "repo_path" in spec.input_class.model_fields:
                    payload["repo_path"] = str(repo_path)
                output = runner(
                    _case_spec(spec, case),
                    payload,
                    trace,
                    repo_path=repo_path,
                    pipeline_run_id=eval_run_id,
                    tag=tag,
                    budget=budget,
                )
                actual = output.model_dump(mode="json")
            graded = grade(
                case,
                actual,
                judge,
                repo_path,
                jev_judge=jev_judge,
                rubric_judge=rubric_judge,
                fetcher=fetcher,
            )
        except AuthOrConfigError, BudgetExceeded, UsageError:
            raise
        except FactoryError as exc:
            message = f"{type(exc).__name__}: {exc.message}"
            return CaseResult(case, False, _elapsed(start), error=message, decision=decision)
    return CaseResult(case, graded.passed, _elapsed(start), grade=graded, decision=decision)


def _fill_fixture_refs(payload: Mapping[str, Any], repo_path: Path) -> tuple[dict[str, Any], bool]:
    """Replace each `$fixture` value of `payload` with the fixture's diff or changed files.

    The diff is read as the review panel reads it, cut at `MAX_OUTPUT_BYTES`, and the files are
    the ones it names, one per line. Also returns whether the diff was cut, False when no value
    refers to it.
    """
    filled = dict(payload)
    refs = {
        key: value[FIXTURE_REF]
        for key, value in payload.items()
        if isinstance(value, dict) and FIXTURE_REF in value
    }
    if not refs:
        return filled, False
    diff = show_diff(repo_path, FIXTURE_BASE, FIXTURE_HEAD, max_bytes=MAX_OUTPUT_BYTES)
    for key, content in refs.items():
        filled[key] = diff if content == "diff" else "\n".join(changed_files(diff))
    last_line = diff.rstrip("\n").rpartition("\n")[2]
    return filled, bool(_CUT_MARKER.match(last_line))


def _decision_name(decision: Decision) -> str:
    """Name a gate decision as a case expects it: `escalate`, `skip`, or `review` for neither."""
    if decision.escalate:
        return "escalate"
    return "skip" if decision.skip else "review"


def _jev_answers(output: JevOutput) -> dict[str, Any]:
    """Flatten a Jev output to what each question picked, so the checks read `verdict` like any other."""
    picked: dict[str, Any] = {}
    for question_id, answer in output:
        dumped = answer.model_dump(mode="json")
        picked[question_id] = next(dumped[key] for key in ("answer", "level", "p") if key in dumped)
    return picked


def _private_copy(repo_path: Path, scratch: Path) -> Path:
    """Copy the fixture repository into `scratch` so an agent that writes cannot disturb a sibling."""
    copy = scratch / repo_path.name
    shutil.copytree(repo_path, copy, symlinks=True)
    return copy


def _elapsed(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _judge_json(verdict: JudgeVerdict) -> dict[str, Any]:
    return {"passed": verdict.passed, "score": verdict.score, "reason": verdict.reason}


def _case_spec(spec: LLMSpec, case: Case) -> LLMSpec:
    """The spec to run `case` with: a `citations_resolve` case's `allow_domains` replace the
    manifest's `tools.web.allow_domains`, so the agent may fetch exactly the hosts the check
    accepts. A case without that check runs with the manifest's list."""
    check = case.expected.citations_resolve
    return spec if check is None else spec.with_web_domains(check.allow_domains)


def parse_ref(ref: str) -> tuple[str, str | None]:
    """Split `name[@range]` into the name and the range, which is None when omitted."""
    name, at, version_range = ref.partition("@")
    if not name or (at and not version_range):
        raise UsageError(f"invalid agent reference {ref!r}", hint="use `name@^MAJOR.MINOR`")
    return name, (version_range if at else None)


def resolve_ref(ref: str) -> LLMSpec | JevSpec:
    """Resolve `name@^M.m`, or the highest version of `name` when the range is omitted."""
    name, version_range = parse_ref(ref)
    if version_range is None:
        versions = [Version(found.version) for found in _list_agents() if found.name == name]
        if not versions:
            raise AgentNotFound(f"no agent named {name!r}", hint="run `factory list`")
        highest = max(versions)
        version_range = f"^{highest.major}.{highest.minor}"
    return resolve(name, version_range)


def command(ref: str, *, as_json: bool, workers: int = DEFAULT_WORKERS) -> int:
    """Run `factory eval`: evaluate the agent `ref` and report it.

    The table goes to stderr. Under `as_json` stdout gets exactly one JSON document, also when
    the pass rate is below the minimum. Returns 0 when the report passed (`evals.min_pass_rate`,
    and `evals.min_recall_risky` when set) and 1 when it did not.
    """
    import json

    if workers < 1:
        raise UsageError(f"--workers must be at least 1, not {workers}")
    spec = resolve_ref(ref)
    cases = load_cases(spec.directory / CASES_FILE)
    with Trace() as trace:
        report = evaluate(spec, cases, trace, workers=workers)
    print(report.table(), file=sys.stderr)
    if as_json:
        print(json.dumps(report.to_json()))
    return ExitCode.OK if report.passed else ExitCode.ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """Run `python -m evals.run <name>[@^M.m] [--json] [--workers N]` as `factory eval`."""
    from factory.cli import main as factory_main

    return factory_main(["eval", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    sys.exit(main())
