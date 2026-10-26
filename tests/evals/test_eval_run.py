"""Tests for evals/run.py: `evaluate`, the report document, and `factory eval`."""

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import evals.run as eval_run
from evals.cases import CASES_FILE, Case, Expected, load_cases
from evals.graders.llm_judge import JudgeVerdict
from evals.run import EvalReport, evaluate
from factory.cli import main
from factory.exit import ExitCode, UsageError
from fixtures.build import FixtureNotFound
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget, BudgetExceeded
from runtime.errors import RefusalError
from runtime.trace import Trace

GOLDEN = Path(__file__).parent / "golden" / "code-reviewer-eval.json"
SUMMARY = "A change."


def _finding(file: str, start: int, end: int, severity: str) -> dict[str, Any]:
    return {
        "file": file,
        "line_start": start,
        "line_end": end,
        "severity": severity,
        "category": "logic",
        "title": "Problem",
        "body": "It is wrong.",
    }


# What a good reviewer answers for each fixture of the code-reviewer cases.
GOOD: dict[str, dict[str, Any]] = {
    "off-by-one": {
        "verdict": "request_changes",
        "findings": [_finding("series/rolling.py", 12, 13, "major")],
    },
    "clean-refactor": {"verdict": "approve", "findings": []},
    "untested-change": {
        "verdict": "approve",
        "findings": [_finding("pricing/discount.py", 4, 8, "minor")],
    },
    "mixed": {
        "verdict": "request_changes",
        "findings": [
            _finding("shop/shipping.py", 12, 12, "major"),
            _finding("shop/format.py", 6, 7, "info"),
        ],
    },
}


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("code-reviewer", "^1.2")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENTS_DIR / "code-reviewer" / CASES_FILE)


@pytest.fixture
def trace(tmp_path: Path) -> Trace:
    return Trace(tmp_path / "traces.duckdb")


@pytest.fixture
def fixture_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / "repos" / name for name in GOOD}
    for path in paths.values():
        path.mkdir(parents=True)
    return paths


class FakeRunner:
    """Stands in for `runtime.runner.run`: answers per fixture and records every call."""

    def __init__(self, spec: LLMSpec, answers: Mapping[str, Any] = GOOD) -> None:
        self.spec = spec
        self.answers = dict(answers)
        self.calls: list[dict[str, Any]] = []
        self.threads: set[str] = set()

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        self.calls.append({"spec": spec, "payload": dict(payload), **kwargs})
        self.threads.add(threading.current_thread().name)
        answer = self.answers[Path(kwargs["repo_path"]).name]
        if isinstance(answer, Exception):
            raise answer
        return spec.output_class.model_validate({"summary": SUMMARY} | answer)


class FakeJudge:
    def __init__(self, passed: bool = True, score: float = 0.9) -> None:
        self.verdict = JudgeVerdict(passed=passed, score=score, reason="judged")
        self.calls = 0

    def __call__(self, expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        self.calls += 1
        return self.verdict


def _evaluate(
    spec: LLMSpec,
    cases: list[Case],
    trace: Trace,
    fixture_paths: dict[str, Path],
    runner: FakeRunner,
    judge: FakeJudge | None = None,
    **kwargs: Any,
) -> EvalReport:
    return evaluate(
        spec,
        cases,
        trace,
        runner=runner,
        judge=judge or FakeJudge(),
        fixture_paths=fixture_paths,
        eval_run_id="eval-run-1",
        **kwargs,
    )


# --- the pass direction


def test_good_answers_pass_every_case(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    judge = FakeJudge()
    report = _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec), judge)
    assert [r.passed for r in report.results] == [True] * 4
    assert (report.passed_count, report.total, report.pass_rate) == (4, 4, 1.0)
    assert report.passed
    assert judge.calls == 4


def test_each_case_runs_through_the_runner_with_the_tag_and_the_fixture_path(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    runner = FakeRunner(spec)
    _evaluate(spec, cases, trace, fixture_paths, runner, workers=1)
    assert [call["repo_path"] for call in runner.calls] == [fixture_paths[c.fixture] for c in cases]
    first = runner.calls[0]
    assert first["tag"] == "eval:code-reviewer@1.2.0"
    assert first["pipeline_run_id"] == "eval-run-1"
    assert first["payload"] == {
        "base": "base",
        "head": "head",
        "repo_path": str(fixture_paths["off-by-one"]),
    }


def test_a_sandbox_agent_runs_on_a_private_copy_of_the_fixture(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    class Writer(FakeRunner):
        def __call__(
            self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kw: Any
        ) -> BaseModel:
            (Path(kw["repo_path"]) / "scribble.txt").write_text("x")
            return super().__call__(spec, payload, trace, **kw)

    sandboxed = spec.model_copy(update={"sandbox": True})
    runner = Writer(sandboxed)
    _evaluate(sandboxed, cases, trace, fixture_paths, runner, workers=1)
    seen = [Path(call["repo_path"]) for call in runner.calls]
    assert all(path not in fixture_paths.values() for path in seen)
    assert all(not (path / "scribble.txt").exists() for path in fixture_paths.values())
    assert [call["payload"]["repo_path"] for call in runner.calls] == [str(p) for p in seen]


def test_cases_run_on_a_pool_sized_by_workers_and_keep_file_order(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    runner = FakeRunner(spec)
    report = _evaluate(spec, cases, trace, fixture_paths, runner, workers=2)
    assert [r.case.id for r in report.results] == [c.id for c in cases]
    assert len(runner.threads) <= 2


# --- the fail direction


def test_a_wrong_answer_fails_its_case_and_the_rate_falls_below_the_minimum(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    answers = GOOD | {"mixed": {"verdict": "approve", "findings": []}}
    judge = FakeJudge()
    report = _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec, answers), judge)
    failed = [r for r in report.results if not r.passed]
    assert [r.case.id for r in failed] == ["mixed"]
    assert report.pass_rate == 0.75
    assert report.passed  # 0.75 meets the agent's minimum of 0.75
    assert judge.calls == 3  # the failed case was never judged


def test_two_failures_put_the_report_below_the_minimum(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    answers = GOOD | {"mixed": GOOD["clean-refactor"], "off-by-one": GOOD["clean-refactor"]}
    report = _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec, answers))
    assert report.pass_rate == 0.5
    assert not report.passed


def test_a_judge_that_fails_an_answer_fails_the_case(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    report = _evaluate(
        spec, cases, trace, fixture_paths, FakeRunner(spec), FakeJudge(passed=False, score=0.2)
    )
    assert report.passed_count == 0
    assert report.results[0].grade is not None
    assert report.results[0].grade.deterministic.passed


def test_a_typed_agent_failure_fails_only_that_case(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    answers = GOOD | {
        "mixed": RefusalError("declined", category="policy", agent="code-reviewer@1.2.0")
    }
    report = _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec, answers))
    mixed = report.results[-1]
    assert not mixed.passed
    assert mixed.grade is None
    assert mixed.error is not None
    assert mixed.error.startswith("RefusalError:")
    assert report.passed_count == 3


def test_a_usage_error_from_the_runner_ends_the_eval(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    answers = GOOD | {"off-by-one": UsageError("bad payload")}
    with pytest.raises(UsageError):
        _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec, answers), workers=1)


def test_a_budget_error_from_the_runner_ends_the_eval_instead_of_failing_the_case(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    answers = GOOD | {"off-by-one": BudgetExceeded(1.0, 1.1)}
    with pytest.raises(BudgetExceeded):
        _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec, answers), workers=1)


def test_every_case_is_charged_to_one_budget(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    runner = FakeRunner(spec)
    budget = Budget(2.0)
    _evaluate(spec, cases, trace, fixture_paths, runner, budget=budget)
    assert {id(call["budget"]) for call in runner.calls} == {id(budget)}


def test_the_default_budget_is_the_pipeline_default(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    runner = FakeRunner(spec)
    _evaluate(spec, cases, trace, fixture_paths, runner)
    assert runner.calls[0]["budget"].usd == DEFAULT_BUDGET_USD


# --- refusals before any model call


def test_workers_below_one_is_a_usage_error(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    with pytest.raises(UsageError, match="--workers"):
        _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec), workers=0)


def test_an_unknown_fixture_is_not_found_before_any_run(
    spec: LLMSpec, cases: list[Case], trace: Trace
) -> None:
    runner = FakeRunner(spec)
    missing = cases[0].model_copy(update={"fixture": "no-such-fixture"})
    with pytest.raises(FixtureNotFound):
        evaluate(spec, [missing], trace, runner=runner, judge=FakeJudge())
    assert runner.calls == []


# --- the report document


def _normalised(document: dict[str, Any]) -> dict[str, Any]:
    document = json.loads(json.dumps(document))
    document["duration_ms"] = 0
    for case in document["cases"]:
        case["duration_ms"] = 0
    return document


def test_the_json_document_matches_the_golden_file(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    answers = GOOD | {"mixed": {"verdict": "approve", "findings": []}}
    report = _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec, answers))
    document = _normalised(report.to_json())
    assert document == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_document_has_the_documented_keys(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    document = _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec)).to_json()
    assert list(document) == [
        "agent",
        "version",
        "ref",
        "tag",
        "eval_run_id",
        "min_pass_rate",
        "pass_rate",
        "passed",
        "total",
        "passed_count",
        "cost_usd",
        "duration_ms",
        "cases",
    ]
    assert set(document["cases"][0]) == {
        "id",
        "fixture",
        "grader",
        "passed",
        "score",
        "reason",
        "deterministic",
        "judge",
        "grader_fallback",
        "error",
        "duration_ms",
    }


def test_the_table_lists_every_case_and_the_verdict(
    spec: LLMSpec, cases: list[Case], trace: Trace, fixture_paths: dict[str, Path]
) -> None:
    answers = GOOD | {"mixed": {"verdict": "approve", "findings": []}}
    table = _evaluate(spec, cases, trace, fixture_paths, FakeRunner(spec, answers)).table()
    assert "PASS  off-by-one" in table
    assert "FAIL  mixed" in table
    assert "3/4 passed (75%), minimum 75%: ok" in table


# --- agent references


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("code-reviewer", ("code-reviewer", None)), ("code-reviewer@^1.2", ("code-reviewer", "^1.2"))],
)
def test_parse_ref(ref: str, expected: tuple[str, str | None]) -> None:
    assert eval_run.parse_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "@^1", "code-reviewer@"])
def test_parse_ref_rejects_malformed_references(ref: str) -> None:
    with pytest.raises(UsageError):
        eval_run.parse_ref(ref)


def test_an_omitted_range_resolves_the_highest_version() -> None:
    spec = eval_run.resolve_ref("code-reviewer")
    assert (spec.name, spec.version) == ("code-reviewer", "1.2.0")


# --- factory eval


@pytest.fixture
def stub_evaluate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec: LLMSpec, cases: list[Case]
) -> list[dict[str, Any]]:
    """Replace `evaluate` with the fake runner and judge; return the keyword arguments it got."""
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "cli.duckdb"))
    seen: list[dict[str, Any]] = []
    real = eval_run.evaluate
    paths = {name: tmp_path / "repos" / name for name in GOOD}
    for path in paths.values():
        path.mkdir(parents=True)

    def fake(spec_: LLMSpec, cases_: list[Case], trace: Trace, **kwargs: Any) -> EvalReport:
        seen.append(kwargs)
        return real(
            spec_,
            cases_,
            trace,
            runner=FakeRunner(spec_, stub_answers[0]),
            judge=FakeJudge(),
            fixture_paths=paths,
            workers=kwargs["workers"],
            eval_run_id="eval-run-1",
        )

    stub_answers: list[Mapping[str, Any]] = [GOOD]
    monkeypatch.setattr(eval_run, "evaluate", fake)
    seen.append({"answers": stub_answers})
    return seen


def test_factory_eval_json_prints_exactly_one_document_and_exits_0(
    capsys: pytest.CaptureFixture[str], stub_evaluate: list[dict[str, Any]]
) -> None:
    assert main(["eval", "code-reviewer@^1.2", "--json", "--workers", "2"]) == ExitCode.OK
    out, err = capsys.readouterr()
    document = json.loads(out)
    assert document["ref"] == "code-reviewer@1.2.0"
    assert document["passed"] is True
    assert "4/4 passed" in err
    assert stub_evaluate[1]["workers"] == 2


def test_factory_eval_exits_1_below_the_minimum_and_still_prints_the_document(
    capsys: pytest.CaptureFixture[str], stub_evaluate: list[dict[str, Any]]
) -> None:
    stub_evaluate[0]["answers"][0] = GOOD | {
        "mixed": GOOD["clean-refactor"],
        "off-by-one": GOOD["clean-refactor"],
    }
    assert main(["eval", "code-reviewer", "--json"]) == ExitCode.ERROR
    out, err = capsys.readouterr()
    assert json.loads(out)["passed"] is False
    assert "BELOW MINIMUM" in err


def test_factory_eval_without_json_keeps_stdout_empty(
    capsys: pytest.CaptureFixture[str], stub_evaluate: list[dict[str, Any]]
) -> None:
    assert main(["eval", "code-reviewer"]) == ExitCode.OK
    out, err = capsys.readouterr()
    assert out == ""
    assert "PASS  off-by-one" in err


def test_factory_eval_usage_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "code-reviewer", "--workers", "0"]) == ExitCode.USAGE
    assert "--workers" in capsys.readouterr().err


def test_factory_eval_missing_agent_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "no-such-agent@^1.0"]) == ExitCode.NOT_FOUND


def test_factory_eval_agent_without_cases_exits_3_with_the_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["eval", "diff-triage", "--json"]) == ExitCode.NOT_FOUND
    out, err = capsys.readouterr()
    assert out == ""
    assert json.loads(err)["error"]["code"] == 3
