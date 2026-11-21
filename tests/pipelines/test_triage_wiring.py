"""Tests for triage in the review panel and for `factory triage`, with fake Jev and agent runners."""

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from factory.cli import build_parser, main
from factory.exit import ExitCode
from fixtures.build import build
from pipelines import review_panel
from pipelines.review_panel import (
    REVIEWERS,
    Panel,
    PanelResult,
    changed_files,
    resolve_panel,
    resolve_triage,
    run_panel,
)
from registry.jev_spec import JevSpec, ProbabilityGate
from registry.resolver import NotPublished
from runtime import gates
from runtime.budget import Budget, BudgetExceeded
from runtime.trace import Trace, TraceRow
from tests.pipelines.test_review_panel import RUN_ID, TRIAGE_ANSWERS, FakeJev, FakeRunner

SKIPPABLE = {**TRIAGE_ANSWERS, "needs_review": {"p": 0.02}}
"""Answers under which a skip gate on `needs_review` below 0.1 fires."""

ALL_REVIEWERS = {name for name, _ in REVIEWERS}


class RecordingTrace(Trace):
    """A real `Trace` that also keeps the rows written to it, which `Trace` cannot read back."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.rows: list[TraceRow] = []

    def record(self, row: TraceRow) -> None:
        """Write the row and keep it."""
        super().record(row)
        self.rows.append(row)


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    (fixture,) = build(["mixed"], dest=tmp_path_factory.mktemp("repos"))
    return fixture.path


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[RecordingTrace]:
    with RecordingTrace(tmp_path / "traces.duckdb") as opened:
        yield opened


def _with_skip_gate(spec: JevSpec) -> JevSpec:
    """The real triage spec plus the skip gate #20 will add, so `skip=True` can be produced."""
    calm = ProbabilityGate(
        name="calm", question="needs_review", rule="p_below", value=0.1, action="skip"
    )
    return spec.model_copy(update={"gates": (calm, *spec.gates)})


@pytest.fixture(scope="module")
def skipping_panel() -> Panel:
    panel = resolve_panel(allow_unpublished=True)
    assert panel.triage is not None
    return Panel(panel.reviewers, panel.judge, _with_skip_gate(panel.triage))


@pytest.fixture(scope="module")
def panel() -> Panel:
    return resolve_panel(allow_unpublished=True)


def _run(
    panel: Panel, repo: Path, trace: Trace, jev: FakeJev, runner: FakeRunner | None = None
) -> tuple[PanelResult, FakeRunner]:
    runner = runner or FakeRunner()
    result = run_panel(
        panel,
        repo,
        "base",
        "head",
        trace,
        runner=runner,
        jev_runner=jev,
        pipeline_run_id=RUN_ID,
    )
    return result, runner


def _triage_rows(trace: RecordingTrace) -> list[TraceRow]:
    return [row for row in trace.rows if row.tag == review_panel.TRIAGE_TAG]


# --- the panel resolves triage


def test_the_panel_pins_diff_triage_to_the_0_4_range(panel: Panel) -> None:
    assert panel.triage is not None
    assert panel.triage.address == "diff-triage@0.4.0"
    assert review_panel.TRIAGE == ("diff-triage", "^0.4")


def test_triage_false_leaves_the_triage_agent_out() -> None:
    assert resolve_panel(allow_unpublished=True, triage=False).triage is None


def test_diff_triage_is_not_published_yet() -> None:
    with pytest.raises(NotPublished):
        resolve_triage()


def test_changed_files_come_from_the_diff_headers() -> None:
    diff = "diff --git a/old.py b/new.py\n+x\ndiff --git a/dir/b.md b/dir/b.md\n-y\n"
    assert changed_files(diff) == ["new.py", "dir/b.md"]


# --- route only, the default for every caller but `factory review`


def test_a_caller_that_does_not_act_runs_every_reviewer_although_the_decision_says_skip(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev(SKIPPABLE)
    result, runner = _run(skipping_panel, repo, trace, jev)
    assert result.triage.route_only is True
    assert result.triage.may_act is False
    assert result.triage.outcome == "full"
    assert result.triage.decision == gates.Decision(
        skip=True, escalate=False, route=("code-reviewer", "security-reviewer"), reason="calm"
    )
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.ship is True


def test_the_decision_and_probabilities_are_logged_in_a_trace_row(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    _run(skipping_panel, repo, trace, FakeJev(SKIPPABLE))
    (row,) = _triage_rows(trace)
    assert row.kind == "jev"
    assert row.agent_ref == "diff-triage@0.4.0"
    assert row.pipeline_run_id == RUN_ID
    assert row.decision_json == {
        "skip": True,
        "escalate": False,
        "route": ["code-reviewer", "security-reviewer"],
        "reason": "calm",
        "route_only": True,
        "may_act": False,
        "sampled": False,
        "truncated": False,
    }
    assert row.output_json == SKIPPABLE
    assert row.error is None


def test_triage_is_called_with_the_files_and_the_diff_under_the_pipeline_run_id(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev()
    _run(panel, repo, trace, jev)
    (call,) = jev.calls_to("diff-triage")
    assert call["spec"].address == "diff-triage@0.4.0"
    assert call["pipeline_run_id"] == RUN_ID
    assert call["payload"]["files"].splitlines() == changed_files(call["payload"]["diff"])
    assert call["payload"]["files"]
    assert call["payload"]["diff"].startswith("diff --git")


def test_triage_runs_before_any_reviewer(panel: Panel, repo: Path, trace: RecordingTrace) -> None:
    _run(panel, repo, trace, FakeJev())
    agents = [row.agent_ref.split("@")[0] for row in trace.rows]
    assert agents[0] == "diff-triage"


def test_the_jev_cost_is_in_the_pipeline_total(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _ = _run(panel, repo, trace, FakeJev(cost_usd=0.01))
    # Two Jev calls at $0.01: triage and the judge gate.
    assert result.total_cost_usd == pytest.approx(1.27)
    assert trace.cost_by_pipeline_run(RUN_ID) == pytest.approx(1.27)


def test_the_decision_row_adds_no_cost(panel: Panel, repo: Path, trace: RecordingTrace) -> None:
    result, _ = _run(panel, repo, trace, FakeJev(cost_usd=0.0))
    assert result.total_cost_usd == pytest.approx(1.25)


def test_the_document_carries_the_triage_entry(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _ = _run(skipping_panel, repo, trace, FakeJev(SKIPPABLE))
    document = json.loads(json.dumps(result.to_json()))
    assert list(document["triage"]) == [
        "status", "ref", "decision", "probabilities", "truncated", "route_only", "may_act",
        "sampled", "outcome", "error",
    ]  # fmt: skip
    assert document["triage"]["status"] == "ok"
    assert document["triage"]["decision"]["skip"] is True
    assert document["triage"]["route_only"] is True


def test_the_text_report_says_the_reviewers_ran_anyway(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _ = _run(skipping_panel, repo, trace, FakeJev(SKIPPABLE))
    assert "triage: calm (skip=True" in result.text()
    assert "route only, every reviewer ran" in result.text()


def _act(
    panel: Panel,
    repo: Path,
    trace: Trace,
    jev: FakeJev,
    *,
    sampled: bool = False,
    runner: FakeRunner | None = None,
) -> tuple[PanelResult, FakeRunner, list[bool]]:
    """Run the panel the way `factory review` does, with a sampler that answers `sampled` and
    records each draw."""
    runner = runner or FakeRunner()
    draws: list[bool] = []

    def sample() -> bool:
        draws.append(sampled)
        return sampled

    result = run_panel(
        panel,
        repo,
        "base",
        "head",
        trace,
        runner=runner,
        jev_runner=jev,
        pipeline_run_id=RUN_ID,
        act_on_triage=True,
        sample=sample,
    )
    return result, runner, draws


# --- acting on a skip


def test_an_acting_skip_runs_no_reviewer_no_judge_gate_and_no_judge(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev(SKIPPABLE)
    result, runner, draws = _act(skipping_panel, repo, trace, jev)
    assert draws == [False]
    assert runner.calls == []
    assert [call["spec"].name for call in jev.calls] == ["diff-triage"]
    assert result.skipped is True
    assert result.ship is True
    assert result.reviewers == ()
    assert result.judgement is None
    assert result.assessment is None
    assert result.triage.outcome == "skipped"
    assert result.triage.route_only is False
    assert result.triage.may_act is True
    assert result.gate.status == "triage_skipped"


def test_an_acting_skip_is_logged_as_allowed_to_act_and_not_sampled(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    _act(skipping_panel, repo, trace, FakeJev(SKIPPABLE))
    (row,) = _triage_rows(trace)
    assert row.decision_json is not None
    assert (
        row.decision_json["skip"],
        row.decision_json["route_only"],
        row.decision_json["may_act"],
        row.decision_json["sampled"],
    ) == (True, False, True, False)


def test_a_skipped_run_costs_only_the_triage_call(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _, _ = _act(skipping_panel, repo, trace, FakeJev(SKIPPABLE, cost_usd=0.01))
    assert result.total_cost_usd == pytest.approx(0.01)


def test_a_skipped_run_says_so_in_the_document(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _, _ = _act(skipping_panel, repo, trace, FakeJev(SKIPPABLE))
    document = json.loads(json.dumps(result.to_json()))
    assert document["reviewers"] == []
    assert document["judge"] is None
    assert document["weighted_score"] is None
    assert document["ship"] is True
    assert document["judge_gate"]["status"] == "triage_skipped"
    assert document["triage"]["outcome"] == "skipped"
    assert document["triage"]["sampled"] is False


def test_a_skipped_run_never_reads_as_reviewers_that_found_nothing(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _, _ = _act(skipping_panel, repo, trace, FakeJev(SKIPPABLE))
    text = result.text()
    assert "skipped the review, no reviewer ran" in text
    assert "judge gate: not run, triage skipped the review" in text
    assert (
        "ship: triage skipped the review (calm: needs_review p=0.020); no reviewer or judge ran. "
        "Rerun with --no-triage to force a review"
    ) in text


def test_the_real_skip_gate_skips_too(panel: Panel, repo: Path, trace: RecordingTrace) -> None:
    result, runner, _ = _act(panel, repo, trace, FakeJev(SKIPPABLE))
    assert result.triage.decision is not None
    assert result.triage.decision.reason == "low_risk"
    assert result.skipped is True
    assert runner.calls == []
    assert "low_risk: needs_review p=0.020" in result.text()


# --- the sample of would-skip runs


def test_the_sample_rate_is_five_percent() -> None:
    assert review_panel.SKIP_SAMPLE_RATE == 0.05


@pytest.mark.parametrize(("draw", "sampled"), [(0.0, True), (0.049, True), (0.05, False)])
def test_draw_sample_is_true_below_the_rate(
    monkeypatch: pytest.MonkeyPatch, draw: float, sampled: bool
) -> None:
    monkeypatch.setattr(review_panel.random, "random", lambda: draw)
    assert review_panel.draw_sample() is sampled


def test_a_sampled_skip_runs_the_full_panel(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev(SKIPPABLE)
    result, runner, draws = _act(skipping_panel, repo, trace, jev, sampled=True)
    assert draws == [True]
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert [call["spec"].name for call in runner.calls].count("judge") == 1
    assert result.skipped is False
    assert result.triage.sampled is True
    assert result.triage.outcome == "full"
    assert "sampled, the full panel ran to check the skip" in result.text()
    (row,) = _triage_rows(trace)
    assert row.decision_json is not None
    assert row.decision_json["sampled"] is True


def test_the_sample_is_drawn_only_for_a_run_that_would_skip(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _, draws = _act(panel, repo, trace, FakeJev(), sampled=True)
    assert draws == []
    assert result.triage.sampled is False


def test_a_caller_that_does_not_act_draws_no_sample(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    draws: list[bool] = []
    run_panel(
        skipping_panel,
        repo,
        "base",
        "head",
        trace,
        runner=FakeRunner(),
        jev_runner=FakeJev(SKIPPABLE),
        pipeline_run_id=RUN_ID,
        sample=lambda: draws.append(True) or True,
    )
    assert draws == []


# --- acting on a route


def test_an_acting_route_runs_only_the_reviewers_it_names(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev()
    result, runner, _ = _act(panel, repo, trace, jev)
    assert result.triage.decision is not None
    assert result.triage.decision.route == ("code-reviewer", "security-reviewer")
    assert runner.reviewers_called() == {"code-reviewer", "security-reviewer"}
    assert [reviewer.spec.name for reviewer in result.reviewers] == [
        "code-reviewer",
        "security-reviewer",
    ]
    assert result.triage.outcome == "routed"
    assert "only the reviewers on the route ran" in result.text()
    (judge_call,) = [call for call in runner.calls if call["spec"].name == "judge"]
    assert "perf-reviewer" not in json.dumps(judge_call["payload"])
    assert "security-reviewer" in json.dumps(judge_call["payload"])


def test_a_route_to_all_reviewers_runs_every_reviewer(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    answers = {**TRIAGE_ANSWERS, "reviewers": {**TRIAGE_ANSWERS["reviewers"], "answer": "all"}}
    result, runner, _ = _act(panel, repo, trace, FakeJev(answers))
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.triage.outcome == "full"
    assert "; every reviewer ran" in result.text()


def test_an_escalation_runs_every_reviewer_when_acting(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    unsure = {**TRIAGE_ANSWERS["reviewers"], "confidence": 0.3}
    result, runner, draws = _act(
        panel, repo, trace, FakeJev({**TRIAGE_ANSWERS, "reviewers": unsure})
    )
    assert result.triage.decision is not None
    assert result.triage.decision.escalate is True
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.triage.outcome == "full"
    assert draws == []


def test_a_jev_failure_runs_every_reviewer_when_acting(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, runner, _ = _act(panel, repo, trace, FakeJev(error=RuntimeError("boom")))
    assert result.triage.status == "jev_error"
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.skipped is False


def test_a_truncated_input_runs_every_reviewer_when_acting(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, runner, draws = _act(skipping_panel, repo, trace, FakeJev(SKIPPABLE, truncated=True))
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.skipped is False
    assert draws == []


def test_a_manifest_without_the_flag_never_acts(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    assert skipping_panel.triage is not None
    flag_off = skipping_panel.triage.model_copy(update={"act_on_decision": False})
    panel = Panel(skipping_panel.reviewers, skipping_panel.judge, flag_off)
    result, runner, draws = _act(panel, repo, trace, FakeJev(SKIPPABLE))
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.triage.route_only is True
    assert result.triage.may_act is True
    assert draws == []
    assert "route only, every reviewer ran" in result.text()


def _triaged(route: tuple[str, ...], **changes: Any) -> review_panel.TriageResult:
    decision = gates.Decision(skip=False, escalate=False, route=route, reason="r")
    result = review_panel.TriageResult("ok", None, decision, {}, False, None, route_only=False)
    return dataclasses.replace(result, **changes)


@pytest.mark.parametrize(
    "route",
    [
        (),
        ("code-reviewer", "nobody"),
        ("code-reviewer", "security-reviewer", "perf-reviewer"),
    ],
    ids=["empty", "unknown", "all"],
)
def test_select_runs_every_reviewer_unless_the_route_is_a_proper_subset(
    panel: Panel, route: tuple[str, ...]
) -> None:
    assert _triaged(route).select(panel.reviewers) == panel.reviewers


def test_select_keeps_the_panel_order(panel: Panel) -> None:
    chosen = _triaged(("perf-reviewer", "code-reviewer")).select(panel.reviewers)
    assert [spec.name for spec in chosen] == ["code-reviewer", "perf-reviewer"]


@pytest.mark.parametrize(
    "changes",
    [{"route_only": True}, {"status": "jev_error"}, {"truncated": True}, {"sampled": True}],
    ids=["route-only", "jev-error", "truncated", "sampled"],
)
def test_select_runs_every_reviewer_when_the_route_may_not_be_trusted(
    panel: Panel, changes: dict[str, Any]
) -> None:
    assert _triaged(("code-reviewer",), **changes).select(panel.reviewers) == panel.reviewers


# --- truncation


def test_a_truncated_jev_input_never_skips(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, runner = _run(skipping_panel, repo, trace, FakeJev(SKIPPABLE, truncated=True))
    assert result.triage.truncated is True
    assert result.triage.decision == gates.Decision(
        skip=False, escalate=True, route=(), reason="truncated"
    )
    assert runner.reviewers_called() == ALL_REVIEWERS
    (row,) = _triage_rows(trace)
    assert row.decision_json["truncated"] is True


def test_a_diff_the_tool_cut_counts_as_truncated(
    skipping_panel: Panel, repo: Path, trace: RecordingTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cut = "diff --git a/a.py b/a.py\n+x\n[truncated: 90 bytes dropped, limit 100 bytes]"
    monkeypatch.setattr(review_panel, "show_diff", lambda *args, **kwargs: cut)
    result, _ = _run(skipping_panel, repo, trace, FakeJev(SKIPPABLE))
    assert result.triage.truncated is True
    assert result.triage.decision is not None
    assert result.triage.decision.skip is False
    assert result.triage.decision.reason == "truncated"


# --- a failed Jev call


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), ValueError("no"), KeyError("TYPESAFE_API_KEY")]
)
def test_a_jev_failure_escalates_and_the_panel_goes_on(
    panel: Panel, repo: Path, trace: RecordingTrace, error: Exception
) -> None:
    result, runner = _run(panel, repo, trace, FakeJev(error=error))
    assert result.triage.status == "jev_error"
    assert result.triage.decision == gates.Decision(
        skip=False, escalate=True, route=(), reason="jev_error"
    )
    assert result.triage.probabilities is None
    assert type(error).__name__ in (result.triage.error or "")
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.ship is True


def test_a_jev_failure_is_logged_with_its_error(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    _run(panel, repo, trace, FakeJev(error=RuntimeError("boom")))
    (row,) = _triage_rows(trace)
    assert row.decision_json["reason"] == "jev_error"
    assert row.decision_json["escalate"] is True
    assert row.error == "RuntimeError: boom"
    assert row.output_json is None


# --- the budget


def test_triage_is_charged_to_the_panel_budget(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev()
    budget = Budget(3.0)
    run_panel(
        panel,
        repo,
        "base",
        "head",
        trace,
        runner=FakeRunner(),
        jev_runner=jev,
        pipeline_run_id=RUN_ID,
        budget=budget,
    )
    (call,) = jev.calls_to("diff-triage")
    assert call["budget"] is budget


def test_a_triage_over_budget_ends_the_panel_before_any_reviewer(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    runner = FakeRunner()
    with pytest.raises(BudgetExceeded):
        _run(panel, repo, trace, FakeJev(error=BudgetExceeded(1.0, 1.2)), runner)
    assert runner.calls == []


# --- --no-triage


def test_a_panel_without_triage_never_calls_jev(repo: Path, trace: RecordingTrace) -> None:
    jev = FakeJev()
    result, runner = _run(resolve_panel(allow_unpublished=True, triage=False), repo, trace, jev)
    assert jev.calls_to("diff-triage") == []
    assert _triage_rows(trace) == []
    assert runner.reviewers_called() == ALL_REVIEWERS
    assert result.triage.status == "disabled"
    assert result.to_json()["triage"] == {
        "status": "disabled",
        "ref": None,
        "decision": None,
        "probabilities": None,
        "truncated": False,
        "route_only": True,
        "may_act": False,
        "sampled": False,
        "outcome": "full",
        "error": None,
    }
    assert result.text().splitlines()[1] == "triage: disabled"


# --- the command line


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Point the CLI at a scratch trace store and fakes; returns them by key."""
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fakes = {"runner": FakeRunner(), "jev": FakeJev()}
    monkeypatch.setattr(review_panel, "run", fakes["runner"])
    monkeypatch.setattr(review_panel, "run_jev", fakes["jev"])
    from pipelines import triage

    monkeypatch.setattr(triage, "run_jev", fakes["jev"])
    return fakes


def _args(command: str, repo: Path, *extra: str) -> list[str]:
    return [command, str(repo), "--base", "base", "--head", "head", "--allow-unpublished", *extra]


def test_the_parser_knows_triage_and_no_triage() -> None:
    parser = build_parser()
    triage = parser.parse_args(["triage", "r", "--base", "b", "--head", "h", "--json"])
    assert (triage.command, triage.repo_path, triage.base, triage.head, triage.json) == (
        "triage", "r", "b", "h", True
    )  # fmt: skip
    review = parser.parse_args(["review", "r", "--base", "b", "--head", "h", "--no-triage"])
    assert review.no_triage is True
    assert parser.parse_args(["review", "r", "--base", "b", "--head", "h"]).no_triage is False


def test_review_runs_triage_by_default(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    assert main(_args("review", repo, "--json")) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["triage"]["status"] == "ok"
    assert len(cli["jev"].calls_to("diff-triage")) == 1


def test_review_no_triage_bypasses_the_jev_call(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    assert main(_args("review", repo, "--json", "--no-triage")) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["triage"]["status"] == "disabled"
    assert cli["jev"].calls_to("diff-triage") == []
    assert len(cli["runner"].calls) == 4


def test_review_survives_a_failing_jev_call(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.setattr(review_panel, "run_jev", FakeJev(error=RuntimeError("down")))
    assert main(_args("review", repo, "--json")) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["triage"]["decision"]["reason"] == "jev_error"
    assert len(cli["runner"].calls) == 4


def test_triage_json_prints_one_document_and_runs_no_reviewer(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    assert main(_args("triage", repo, "--json")) == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert list(document) == [
        "pipeline_run_id", "base", "head", "status", "ref", "decision", "probabilities",
        "truncated", "route_only", "may_act", "sampled", "outcome", "error",
    ]  # fmt: skip
    assert document["decision"]["route"] == ["code-reviewer", "security-reviewer"]
    assert document["probabilities"] == TRIAGE_ANSWERS
    assert "triage: reviewers=code-security" in captured.err
    assert cli["runner"].calls == []
    assert cli["jev"].calls[0]["pipeline_run_id"] == document["pipeline_run_id"]


def test_triage_without_json_prints_the_decision_and_the_probabilities(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    assert main(_args("triage", repo)) == ExitCode.OK
    out = capsys.readouterr().out
    assert "triage: reviewers=code-security" in out
    assert "needs_review: yes 0.80" in out
    assert "reviewers: code 0.10, code-security 0.70, code-perf 0.10, all 0.10" in out


def test_triage_exits_1_with_the_escalation_when_jev_fails(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    from pipelines import triage

    monkeypatch.setattr(triage, "run_jev", FakeJev(error=RuntimeError("down")))
    assert main(_args("triage", repo, "--json")) == ExitCode.ERROR
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "jev_error"
    assert document["decision"]["escalate"] is True
    assert document["error"] == "RuntimeError: down"


def test_triage_with_an_unpublished_agent_exits_3(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    args = ["triage", str(repo), "--base", "base", "--head", "head"]
    assert main(args) == ExitCode.NOT_FOUND
    assert cli["jev"].calls == []


def test_triage_with_an_unknown_ref_exits_3(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    args = ["triage", str(repo), "--base", "base", "--head", "nope", "--allow-unpublished"]
    assert main(args) == ExitCode.NOT_FOUND


def test_triage_missing_flags_exit_2(repo: Path) -> None:
    assert main(["triage", str(repo), "--base", "base"]) == ExitCode.USAGE


def test_review_acts_on_a_skip_and_exits_0(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.setattr(review_panel, "run_jev", FakeJev(SKIPPABLE))
    monkeypatch.setattr(review_panel.random, "random", lambda: 0.5)
    assert main(_args("review", repo, "--json")) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["triage"]["outcome"] == "skipped"
    assert document["triage"]["may_act"] is True
    assert document["reviewers"] == []
    assert document["ship"] is True
    assert cli["runner"].calls == []


def test_review_no_triage_runs_every_reviewer_on_a_diff_triage_would_skip(
    capsys: pytest.CaptureFixture[str],
    cli: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    monkeypatch.setattr(review_panel, "run_jev", FakeJev(SKIPPABLE))
    assert main(_args("review", repo, "--json", "--no-triage")) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["triage"]["status"] == "disabled"
    assert len(cli["runner"].calls) == 4
