"""Tests for pipelines/review_panel.py: `run_panel`, the pipeline document and `factory review`."""

import asyncio
import json
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.cli import main
from factory.exit import ExitCode, UsageError
from fixtures.build import build
from pipelines import review_panel
from pipelines.ids import new_pipeline_run_id
from pipelines.review_panel import (
    JUDGE,
    REVIEWERS,
    AllReviewersFailed,
    Panel,
    PanelResult,
    resolve_panel,
    run_panel,
)
from registry.jev_spec import JevOutput, JevSpec
from registry.spec import LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget, BudgetExceeded
from runtime.errors import RefusalError
from runtime.trace import Trace, TraceRow
from tools import ToolNotFoundError

GOLDEN = Path(__file__).parent / "golden" / "review-panel.json"
RUN_ID = "pipeline-run-1"
CODE, SECURITY, PERF = (f"{name}@" for name, _ in REVIEWERS)

SHIPPING = {"correctness_risk": 0.1, "security_risk": 0.0, "test_coverage": 0.8,
            "maintainability": 0.9}  # fmt: skip
BLOCKING = {"correctness_risk": 0.9, "security_risk": 0.5, "test_coverage": 0.2,
            "maintainability": 0.4}  # fmt: skip


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


def _review(*findings: dict[str, Any]) -> dict[str, Any]:
    verdict = "request_changes" if findings else "approve"
    return {"findings": list(findings), "summary": "A change.", "verdict": verdict}


def _judgement(scores: Mapping[str, float]) -> dict[str, Any]:
    return {
        "scores": {
            name: {"value": value, "justification": "Checked."} for name, value in scores.items()
        },
        "merged_findings": [
            _finding("shop/shipping.py", 12, 12, "major") | {"sources": [CODE + "1.2.0"]}
        ],
        "rationale": "The comparison is flipped.",
    }


def _refusal(message: str) -> RefusalError:
    return RefusalError(message, category=None, agent="test-agent@1.0.0")


class FakeRunner:
    """Stands in for `runtime.runner.run`: answers per agent name, traces a row, records calls."""

    def __init__(
        self,
        judgement: Mapping[str, Any] | None = None,
        failing: Mapping[str, Exception] | None = None,
        *,
        together: bool = False,
    ) -> None:
        self.judgement = _judgement(SHIPPING) if judgement is None else dict(judgement)
        self.failing = dict(failing or {})
        self.calls: list[dict[str, Any]] = []
        self.threads: dict[str, str] = {}
        self.barrier = threading.Barrier(len(REVIEWERS), timeout=10) if together else None

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        self.calls.append({"spec": spec, "payload": dict(payload), **kwargs})
        self.threads[spec.name] = threading.current_thread().name
        if self.barrier is not None and spec.name != JUDGE[0]:
            self.barrier.wait()
        if spec.name in self.failing:
            raise self.failing[spec.name]
        trace.record(
            TraceRow(
                run_id=f"row-{spec.name}",
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=dict(payload),
                cost_usd=0.5 if spec.name == JUDGE[0] else 0.25,
            )
        )
        if spec.name == JUDGE[0]:
            return spec.output_class.model_validate(self.judgement)
        findings = (
            [_finding("shop/shipping.py", 12, 12, "major")] if spec.name == "code-reviewer" else []
        )
        return spec.output_class.model_validate(_review(*findings))

    def reviewers_called(self) -> set[str]:
        return {c["spec"].name for c in self.calls if c["spec"].name != JUDGE[0]}


TRIAGE_ANSWERS: dict[str, Any] = {
    "needs_review": {"p": 0.8},
    "reviewers": {
        "answer": "code-security",
        "probs": {"code": 0.1, "code-security": 0.7, "code-perf": 0.1, "all": 0.1},
        "confidence": 0.9,
    },
}
"""What the fake triage agent answers by default: no gate fires, route is code and security."""

GATE_ANSWERS: dict[str, Any] = {
    "clear_cut": {"p": 0.2},
    "verdict": {
        "answer": "request_changes",
        "probs": {"ship": 0.1, "request_changes": 0.8, "unclear": 0.1},
        "confidence": 0.9,
    },
}
"""What the fake judge-gate agent answers by default: not clear-cut, so the judge still runs."""


class FakeJev:
    """Stands in for `runtime.jev_runner.run_jev`: answers fixed, traces a row, records calls."""

    def __init__(
        self,
        answers: Mapping[str, Any] | None = None,
        error: Exception | None = None,
        *,
        truncated: bool = False,
        cost_usd: float = 0.0,
        gate_answers: Mapping[str, Any] | None = None,
    ) -> None:
        self.answers = dict(TRIAGE_ANSWERS if answers is None else answers)
        self.gate_answers = dict(GATE_ANSWERS if gate_answers is None else gate_answers)
        self.error = error
        self.truncated = truncated
        self.cost_usd = cost_usd
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, spec: JevSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> JevOutput:
        self.calls.append({"spec": spec, "payload": dict(payload), **kwargs})
        trace.record(
            TraceRow(
                run_id=f"row-jev-{len(self.calls)}",
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="jev",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=dict(payload),
                cost_usd=self.cost_usd,
                decision_json={"truncated": self.truncated, "model": spec.model},
                error=None if self.error is None else str(self.error),
            )
        )
        if self.error is not None:
            raise self.error
        answers = self.gate_answers if spec.name == "judge-gate" else self.answers
        return spec.output_class.model_validate(answers)

    def calls_to(self, name: str) -> list[dict[str, Any]]:
        """Return the calls made to the Jev agent `name`."""
        return [call for call in self.calls if call["spec"].name == name]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    (fixture,) = build(["mixed"], dest=tmp_path_factory.mktemp("repos"))
    return fixture.path


@pytest.fixture(scope="module")
def panel() -> Panel:
    return resolve_panel(allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


def _run(panel: Panel, repo: Path, trace: Trace, runner: FakeRunner, **kwargs: Any) -> PanelResult:
    kwargs.setdefault("jev_runner", FakeJev())
    return run_panel(
        panel, repo, "base", "head", trace, runner=runner, pipeline_run_id=RUN_ID, **kwargs
    )


# --- resolving the panel


def test_the_panel_resolves_the_pinned_versions(panel: Panel) -> None:
    assert [spec.address for spec in panel.reviewers] == [
        "code-reviewer@1.2.0",
        "security-reviewer@1.1.0",
        "perf-reviewer@0.3.0",
    ]
    assert panel.judge.address == "judge@2.1.0"


def test_reviewers_selects_a_subset_in_panel_order() -> None:
    selected = resolve_panel(["perf-reviewer", "code-reviewer"], allow_unpublished=True)
    assert [spec.name for spec in selected.reviewers] == ["code-reviewer", "perf-reviewer"]


def test_an_unknown_reviewer_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="unknown reviewer 'style-reviewer'"):
        resolve_panel(["code-reviewer", "style-reviewer"], allow_unpublished=True)


def test_an_empty_reviewer_list_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="at least one"):
        resolve_panel([], allow_unpublished=True)


def test_unpublished_versions_do_not_resolve_by_default(tmp_path: Path) -> None:
    from registry.resolver import NotPublished

    with pytest.raises(NotPublished):
        resolve_panel(trace_path=tmp_path / "empty.duckdb")


# --- running it


def test_every_run_shares_the_pipeline_run_id(panel: Panel, repo: Path, trace: Trace) -> None:
    runner = FakeRunner()
    result = _run(panel, repo, trace, runner)
    assert len(runner.calls) == 4
    assert {call["pipeline_run_id"] for call in runner.calls} == {RUN_ID}
    assert result.pipeline_run_id == RUN_ID
    assert result.total_cost_usd == trace.cost_by_pipeline_run(RUN_ID) == 1.25


def test_a_generated_pipeline_run_id_is_used_when_none_is_given(
    panel: Panel, repo: Path, trace: Trace
) -> None:
    runner = FakeRunner()
    result = run_panel(panel, repo, "base", "head", trace, runner=runner)
    assert result.pipeline_run_id == runner.calls[0]["pipeline_run_id"]
    assert result.pipeline_run_id != new_pipeline_run_id()


def test_the_reviewers_run_at_the_same_time(panel: Panel, repo: Path, trace: Trace) -> None:
    # The barrier only opens when all three reviewers are inside `run` together.
    runner = FakeRunner(together=True)
    _run(panel, repo, trace, runner)
    assert len({runner.threads[name] for name, _ in REVIEWERS}) == 3


def test_the_judge_gets_the_reviews_and_the_diff(panel: Panel, repo: Path, trace: Trace) -> None:
    runner = FakeRunner()
    _run(panel, repo, trace, runner)
    judge_call = runner.calls[-1]
    assert judge_call["spec"].name == "judge"
    payload = judge_call["payload"]
    assert list(payload["reviews"]) == [spec.address for spec in panel.reviewers]
    assert payload["failed_reviewers"] == []
    assert payload["diff_context"]["base"] == "base"
    assert "shop/shipping.py" in payload["diff_context"]["diff"]
    assert judge_call["repo_path"] == repo


def test_a_shipping_judgement_ships(panel: Panel, repo: Path, trace: Trace) -> None:
    result = _run(panel, repo, trace, FakeRunner())
    assert result.ship is True
    assert result.assessment is not None
    assert result.assessment.score == pytest.approx(0.4 * 0.9 + 0.25 * 1.0 + 0.2 * 0.8 + 0.15 * 0.9)


def test_a_blocking_judgement_does_not_ship(panel: Panel, repo: Path, trace: Trace) -> None:
    result = _run(panel, repo, trace, FakeRunner(_judgement(BLOCKING)))
    assert result.ship is False
    assert result.to_json()["ship"] is False


def test_a_reviewer_that_raises_is_reported_and_the_panel_goes_on(
    panel: Panel, repo: Path, trace: Trace
) -> None:
    failing = {"security-reviewer": _refusal("declined")}
    runner = FakeRunner(failing=failing)
    result = _run(panel, repo, trace, runner)
    statuses = {r.spec.name: r.to_json() for r in result.reviewers}
    assert statuses["security-reviewer"]["status"] == "failed"
    assert "RefusalError" in statuses["security-reviewer"]["error"]
    assert statuses["code-reviewer"]["status"] == "ok"
    payload = runner.calls[-1]["payload"]
    assert list(payload["reviews"]) == [CODE + "1.2.0", PERF + "0.3.0"]
    assert [f["reviewer"] for f in payload["failed_reviewers"]] == [SECURITY + "1.1.0"]
    assert "declined" in payload["failed_reviewers"][0]["error"]


def test_every_reviewer_failing_raises_without_running_the_judge(
    panel: Panel, repo: Path, trace: Trace
) -> None:
    failing = {name: _refusal(f"{name} declined") for name, _ in REVIEWERS}
    runner = FakeRunner(failing=failing)
    with pytest.raises(AllReviewersFailed, match="every reviewer failed") as raised:
        _run(panel, repo, trace, runner)
    assert "perf-reviewer@0.3.0" in str(raised.value)
    assert "judge" not in {call["spec"].name for call in runner.calls}


def test_a_failing_judge_raises(panel: Panel, repo: Path, trace: Trace) -> None:
    with pytest.raises(RefusalError):
        _run(
            panel,
            repo,
            trace,
            FakeRunner(failing={"judge": _refusal("declined")}),
        )


def test_a_subset_runs_only_those_reviewers(repo: Path, trace: Trace) -> None:
    subset = resolve_panel(["code-reviewer", "perf-reviewer"], allow_unpublished=True)
    runner = FakeRunner()
    result = _run(subset, repo, trace, runner)
    assert runner.reviewers_called() == {"code-reviewer", "perf-reviewer"}
    assert [r.spec.name for r in result.reviewers] == ["code-reviewer", "perf-reviewer"]


def test_an_unknown_ref_raises_not_found_before_any_agent_runs(
    panel: Panel, repo: Path, trace: Trace
) -> None:
    runner = FakeRunner()
    with pytest.raises(ToolNotFoundError):
        run_panel(panel, repo, "base", "no-such-ref", trace, runner=runner)
    assert runner.calls == []


# --- the document


def _normalised(document: dict[str, Any]) -> dict[str, Any]:
    document = json.loads(json.dumps(document))
    document["duration_ms"] = 0
    return document


def test_the_json_document_matches_the_golden_file(panel: Panel, repo: Path, trace: Trace) -> None:
    runner = FakeRunner(failing={"perf-reviewer": _refusal("declined")})
    document = _normalised(_run(panel, repo, trace, runner).to_json())
    assert document == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_document_has_the_documented_keys(panel: Panel, repo: Path, trace: Trace) -> None:
    document = _run(panel, repo, trace, FakeRunner()).to_json()
    assert list(document) == [
        "pipeline_run_id",
        "base",
        "head",
        "reviewers",
        "judge",
        "weighted_score",
        "ship",
        "total_cost_usd",
        "duration_ms",
        "triage",
        "judge_gate",
    ]
    assert list(document["reviewers"][0]) == [
        "agent", "version", "ref", "status", "verdict", "finding_count", "error"
    ]  # fmt: skip
    assert list(document["judge"]) == ["ref", "output"]


# --- factory review


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, FakeRunner]:
    """Point the CLI at a scratch trace store and a fake runner; returns the runner by key."""
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    runner = FakeRunner()
    monkeypatch.setattr(review_panel, "run", runner)
    monkeypatch.setattr(review_panel, "run_jev", FakeJev())
    return {"runner": runner}


def _review_args(repo: Path, *extra: str) -> list[str]:
    return ["review", str(repo), "--base", "base", "--head", "head", "--allow-unpublished", *extra]


def test_review_json_prints_one_document_and_the_prose_goes_to_stderr(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    assert main(_review_args(repo, "--json")) == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ship"] is True
    # The fake triage routes to code and security, so perf-reviewer does not run.
    assert document["total_cost_usd"] == 1.0
    assert document["pipeline_run_id"] == cli["runner"].calls[0]["pipeline_run_id"]
    assert "do not ship" not in captured.err and "ship: weighted score" in captured.err


def test_review_without_json_prints_the_text_report(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    assert main(_review_args(repo)) == ExitCode.OK
    captured = capsys.readouterr()
    assert "judge" not in captured.err
    assert "ship: weighted score" in captured.out
    assert "code-reviewer@1.2.0: request_changes, 1 findings" in captured.out


def test_a_negative_result_still_prints_its_document_and_exits_1(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, FakeRunner],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.setattr(review_panel, "run", FakeRunner(_judgement(BLOCKING)))
    assert main(_review_args(repo, "--json")) == ExitCode.ERROR
    assert json.loads(capsys.readouterr().out)["ship"] is False


def test_reviewers_flag_runs_the_named_subset(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    # Triage acts inside the panel `--reviewers` names, so --no-triage runs exactly those.
    argv = _review_args(repo, "--json", "--reviewers", "code-reviewer,perf-reviewer", "--no-triage")
    assert main(argv) == 0
    document = json.loads(capsys.readouterr().out)
    assert [r["agent"] for r in document["reviewers"]] == ["code-reviewer", "perf-reviewer"]


def test_reviewers_flag_is_narrowed_by_the_triage_route(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    assert main(_review_args(repo, "--json", "--reviewers", "code-reviewer,perf-reviewer")) == 0
    document = json.loads(capsys.readouterr().out)
    assert [r["agent"] for r in document["reviewers"]] == ["code-reviewer"]
    assert document["triage"]["outcome"] == "routed"


def test_a_failed_reviewer_is_in_the_document(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, FakeRunner],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.setattr(
        review_panel,
        "run",
        FakeRunner(failing={"perf-reviewer": _refusal("no")}),
    )
    assert main(_review_args(repo, "--json", "--no-triage")) == ExitCode.OK
    reviewers = {r["agent"]: r for r in json.loads(capsys.readouterr().out)["reviewers"]}
    assert reviewers["perf-reviewer"]["status"] == "failed"


def test_all_reviewers_failing_exits_1_with_only_the_envelope(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, FakeRunner],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    failing = {name: _refusal("no") for name, _ in REVIEWERS}
    monkeypatch.setattr(review_panel, "run", FakeRunner(failing=failing))
    assert main(_review_args(repo, "--json")) == ExitCode.ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip().splitlines()[-1])["error"]["code"] == 1


def test_an_unknown_reviewer_exits_2(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    assert main(_review_args(repo, "--reviewers", "style-reviewer")) == ExitCode.USAGE
    assert "unknown reviewer" in capsys.readouterr().err


def test_missing_flags_exit_2(capsys: pytest.CaptureFixture[str], repo: Path) -> None:
    assert main(["review", str(repo), "--base", "base"]) == ExitCode.USAGE


def test_an_unpublished_agent_exits_3_before_anything_runs(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    args = ["review", str(repo), "--base", "base", "--head", "head", "--json"]
    assert main(args) == ExitCode.NOT_FOUND
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hint" in json.loads(captured.err)["error"]
    assert cli["runner"].calls == []


def test_an_unknown_ref_exits_3(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    args = ["review", str(repo), "--base", "base", "--head", "nope", "--allow-unpublished"]
    assert main(args) == ExitCode.NOT_FOUND


def test_a_missing_key_exits_4(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, FakeRunner],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert main(_review_args(repo)) == ExitCode.AUTH_OR_CONFIG
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert cli["runner"].calls == []


# --- the budget


def test_every_run_shares_one_budget(panel: Panel, repo: Path, trace: Trace) -> None:
    runner = FakeRunner()
    budget = Budget(3.0)
    _run(panel, repo, trace, runner, budget=budget)
    assert {id(call["budget"]) for call in runner.calls} == {id(budget)}


def test_the_default_budget_is_the_pipeline_default(panel: Panel, repo: Path, trace: Trace) -> None:
    runner = FakeRunner()
    _run(panel, repo, trace, runner)
    (budget,) = {id(call["budget"]): call["budget"] for call in runner.calls}.values()
    assert budget.usd == DEFAULT_BUDGET_USD


def test_a_reviewer_that_hits_the_budget_ends_the_panel(
    panel: Panel, repo: Path, trace: Trace
) -> None:
    runner = FakeRunner(failing={"security-reviewer": BudgetExceeded(1.0, 1.2)})
    with pytest.raises(BudgetExceeded):
        _run(panel, repo, trace, runner, budget=Budget(1.0))
    assert JUDGE[0] not in {call["spec"].name for call in runner.calls}
    # The other reviewers finish, so their rows and charges are in before the panel ends.
    assert runner.reviewers_called() == {name for name, _ in REVIEWERS}
    assert trace.cost_by_pipeline_run(RUN_ID) == 0.25 * (len(REVIEWERS) - 1)


def test_the_first_error_in_report_order_is_the_one_raised(
    panel: Panel, repo: Path, trace: Trace
) -> None:
    first, second = BudgetExceeded(1.0, 1.1), BudgetExceeded(1.0, 1.2)
    runner = FakeRunner(failing={"code-reviewer": first, "perf-reviewer": second})
    with pytest.raises(BudgetExceeded) as raised:
        _run(panel, repo, trace, runner, budget=Budget(1.0))
    assert raised.value is first


def test_the_default_runner_runs_the_reviewers_on_arun_on_one_loop(
    panel: Panel, repo: Path, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    started: list[str] = []
    loops: set[int] = set()
    everyone_in = asyncio.Event()

    async def fake_arun(
        spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        loops.add(id(asyncio.get_running_loop()))
        started.append(spec.name)
        if len(started) == len(REVIEWERS):
            everyone_in.set()
        # Only opens when all three reviewers are awaiting together on the loop.
        await asyncio.wait_for(everyone_in.wait(), timeout=10)
        return runner(spec, payload, trace, **kwargs)

    monkeypatch.setattr(review_panel, "_RUN", runner)
    monkeypatch.setattr(review_panel, "arun", fake_arun)
    result = _run(panel, repo, trace, runner)
    assert sorted(started) == sorted(name for name, _ in REVIEWERS)
    assert len(loops) == 1
    assert len(set(runner.threads.values())) == 1
    assert runner.calls[-1]["spec"].name == JUDGE[0]
    assert result.judgement is not None


def test_a_judge_that_hits_the_budget_ends_the_panel(
    panel: Panel, repo: Path, trace: Trace
) -> None:
    runner = FakeRunner(failing={"judge": BudgetExceeded(1.0, 1.2)})
    with pytest.raises(BudgetExceeded):
        _run(panel, repo, trace, runner, budget=Budget(1.0))


def test_budget_usd_sets_the_cap_of_the_whole_review(
    cli: dict[str, FakeRunner], repo: Path
) -> None:
    assert main(_review_args(repo, "--budget-usd", "0.75")) == ExitCode.OK
    budgets = {id(call["budget"]): call["budget"] for call in cli["runner"].calls}
    (budget,) = budgets.values()
    assert budget.usd == 0.75


def test_a_review_over_budget_exits_1_with_the_spend(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path
) -> None:
    cli["runner"].failing["judge"] = BudgetExceeded(0.5, 0.6)
    assert main(_review_args(repo, "--budget-usd", "0.5")) == ExitCode.ERROR
    captured = capsys.readouterr()
    assert "budget of $0.5000 exceeded" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_budget_that_is_not_positive_exits_2_before_anything_runs(
    capsys: pytest.CaptureFixture[str], cli: dict[str, FakeRunner], repo: Path, value: str
) -> None:
    assert main(_review_args(repo, "--budget-usd", value)) == ExitCode.USAGE
    assert cli["runner"].calls == []
