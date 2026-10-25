"""Run an agent's eval cases and gate on its `evals.min_pass_rate`.

`evaluate` builds each case's fixture, runs the agent on it through `runtime.runner.run` with the
trace tag `eval:<name>@<version>`, grades the answer (`evals.graders`), and returns an
`EvalReport`. Cases run on a thread pool; results keep the order of the cases file. `command` is
what `factory eval <name>[@^M.m]` runs, and `python -m evals.run` is the same command.

A case whose agent run fails with a typed error (a refusal, a timeout, invalid output) is a failed
case with that error, not a failed eval. A missing or rejected API key, and a payload the agent
cannot accept, end the whole eval, since every other case would fail the same way.

An agent with `sandbox: true` changes the repository it works on. Its cases share one built
fixture, so each case runs on its own copy, and the `sandbox_tests` grader runs the tests in that
copy after the agent is done.
"""

import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import Version
from pydantic import BaseModel

from evals.cases import CASES_FILE, Case, load_cases
from evals.graders import Deterministic, Grade, ensure_supported, grade
from evals.graders.llm_judge import Judge, JudgeVerdict, LLMJudge
from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from fixtures.build import build
from registry.jev_spec import JevSpec
from registry.resolver import AgentNotFound, resolve
from registry.resolver import list_agents as _list_agents
from registry.spec import LLMSpec
from runtime.runner import run
from runtime.trace import Trace, TraceRow

DEFAULT_WORKERS = 4

type Runner = Callable[..., BaseModel]
"""Anything called like `runtime.runner.run`; tests inject a fake."""


class _SharedTrace(Trace):
    """A view of a `Trace` whose writes are safe to make from the eval's worker threads.

    A `Trace` holds one DuckDB connection and is not thread-safe, so every write goes through the
    wrapped trace under a lock. The view itself never opens a connection.
    """

    def __init__(self, inner: Trace) -> None:
        super().__init__(inner.path)
        self._inner = inner
        self._lock = threading.Lock()

    def record(self, row: TraceRow) -> None:
        """Write one run to the wrapped trace, one thread at a time."""
        with self._lock:
            self._inner.record(row)


@dataclass(frozen=True, slots=True)
class CaseResult:
    """How one case went. `error` is set, and `grade` is None, when the agent run failed."""

    case: Case
    passed: bool
    duration_ms: int
    grade: Grade | None = None
    error: str | None = None

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
    def passed(self) -> bool:
        """Whether the pass rate meets the agent's `evals.min_pass_rate`."""
        return self.pass_rate >= self.min_pass_rate

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
            f"{self.min_pass_rate:.0%}: {verdict}  cost ${self.cost_usd:.4f}  "
            f"{self.duration_ms / 1000:.1f}s"
        )
        return "\n".join(lines)


def evaluate(
    spec: LLMSpec | JevSpec,
    cases: Sequence[Case],
    trace: Trace,
    *,
    runner: Runner = run,
    judge: Judge | None = None,
    workers: int = DEFAULT_WORKERS,
    eval_run_id: str | None = None,
    fixture_paths: Mapping[str, Path] | None = None,
) -> EvalReport:
    """Run `cases` against `spec` and return the report.

    `runner` is called as `runtime.runner.run` is, with `tag` set to `eval:<address>` and the eval's
    id as `pipeline_run_id`; `judge` grades the answers that pass the deterministic checks and
    defaults to an `LLMJudge` on `trace`. `fixture_paths` maps fixture names to built repositories;
    by default every fixture the cases use is built into a temporary directory first, so a missing
    one is a `FixtureNotFound` before any model is called. The input model's `repo_path` field, when
    it has one, is set to the fixture's path.

    Raises `UsageError` for `workers` below 1 or a JevSpec, `NotImplementedError` for a `jev`
    grader, `AuthOrConfigError` for a missing or rejected key, and `FixtureNotFound` or
    `BuildError` from the fixture build.
    """
    if workers < 1:
        raise UsageError(f"--workers must be at least 1, not {workers}")
    if isinstance(spec, JevSpec):
        raise NotImplementedError("evaluating a jev agent arrives with #19")
    for case in cases:
        ensure_supported(case.grader)
    eval_run_id = eval_run_id or str(uuid.uuid4())
    tag = f"eval:{spec.address}"
    start = time.monotonic()
    shared = _SharedTrace(trace)
    own_judge = judge is None
    llm_judge = LLMJudge(shared, tag=tag, pipeline_run_id=eval_run_id) if own_judge else None
    try:
        with tempfile.TemporaryDirectory(prefix="factory-eval-") as scratch:
            paths = fixture_paths or _build_fixtures(cases, Path(scratch))
            results = _run_all(
                spec,
                cases,
                paths,
                shared,
                runner,
                judge if judge is not None else _required(llm_judge),
                workers,
                eval_run_id,
                tag,
            )
    finally:
        if llm_judge is not None:
            llm_judge.close()
    return EvalReport(
        agent=spec.name,
        version=spec.version,
        tag=tag,
        eval_run_id=eval_run_id,
        min_pass_rate=spec.evals.min_pass_rate,
        cost_usd=trace.cost_by_pipeline_run(eval_run_id),
        duration_ms=int((time.monotonic() - start) * 1000),
        results=tuple(results),
    )


def _required(judge: LLMJudge | None) -> LLMJudge:
    assert judge is not None
    return judge


def _build_fixtures(cases: Sequence[Case], dest: Path) -> dict[str, Path]:
    names = sorted({case.fixture for case in cases})
    return {built.name: built.path for built in build(names, dest=dest)}


def _run_all(
    spec: LLMSpec,
    cases: Sequence[Case],
    paths: Mapping[str, Path],
    trace: Trace,
    runner: Runner,
    judge: Judge,
    workers: int,
    eval_run_id: str,
    tag: str,
) -> list[CaseResult]:
    def one(case: Case) -> CaseResult:
        return _run_case(spec, case, paths[case.fixture], trace, runner, judge, eval_run_id, tag)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval") as pool:
        futures = [pool.submit(one, case) for case in cases]
        try:
            return [future.result() for future in futures]
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise


def _run_case(
    spec: LLMSpec,
    case: Case,
    repo_path: Path,
    trace: Trace,
    runner: Runner,
    judge: Judge,
    eval_run_id: str,
    tag: str,
) -> CaseResult:
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="factory-eval-case-") as scratch:
        if spec.sandbox:
            repo_path = _private_copy(repo_path, Path(scratch))
        payload = dict(case.payload)
        if "repo_path" in spec.input_class.model_fields:
            payload["repo_path"] = str(repo_path)
        try:
            output = runner(
                spec, payload, trace, repo_path=repo_path, pipeline_run_id=eval_run_id, tag=tag
            )
            graded = grade(case, output.model_dump(mode="json"), judge, repo_path)
        except AuthOrConfigError, UsageError:
            raise
        except FactoryError as exc:
            message = f"{type(exc).__name__}: {exc.message}"
            return CaseResult(case, False, _elapsed(start), error=message)
    return CaseResult(case, graded.passed, _elapsed(start), grade=graded)


def _private_copy(repo_path: Path, scratch: Path) -> Path:
    """Copy the fixture repository into `scratch` so an agent that writes cannot disturb a sibling."""
    copy = scratch / repo_path.name
    shutil.copytree(repo_path, copy, symlinks=True)
    return copy


def _elapsed(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _judge_json(verdict: JudgeVerdict) -> dict[str, Any]:
    return {"passed": verdict.passed, "score": verdict.score, "reason": verdict.reason}


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
    the pass rate is below the minimum. Returns 0 at or above `evals.min_pass_rate` and 1 below.
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
