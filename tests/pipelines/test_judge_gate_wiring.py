"""Tests for the judge gate in the review panel, with fake Jev and agent runners."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.cli import build_parser, main
from factory.exit import ExitCode
from fixtures.build import build
from pipelines import review_panel
from pipelines.review_panel import (
    GATE_DISABLED,
    JUDGE_GATE_TAG,
    PATH_ESCALATED,
    PATH_SKIPPED,
    REVIEWER_FAILED,
    Panel,
    PanelResult,
    ReviewerResult,
    estimate_judge_cost,
    render_reviews,
    resolve_judge_gate,
    resolve_panel,
    run_panel,
)
from registry.common_schemas import ReviewOutput
from registry.resolver import NotPublished
from runtime import gates
from runtime.budget import Budget, BudgetExceeded
from runtime.trace import Trace, TraceRow
from tests.pipelines.test_review_panel import (
    JUDGE,
    RUN_ID,
    FakeJev,
    FakeRunner,
    _finding,
    _review,
)

CLEAR_SHIP: dict[str, Any] = {
    "clear_cut": {"p": 0.95},
    "verdict": {
        "answer": "ship",
        "probs": {"ship": 0.95, "request_changes": 0.03, "unclear": 0.02},
        "confidence": 0.95,
    },
}
"""Answers under which the gate is sure the change ships."""

CLEAR_BLOCK: dict[str, Any] = {
    "clear_cut": {"p": 0.95},
    "verdict": {
        "answer": "request_changes",
        "probs": {"ship": 0.02, "request_changes": 0.95, "unclear": 0.03},
        "confidence": 0.95,
    },
}
"""Answers under which the gate is sure the change must not ship."""

UNSURE: dict[str, Any] = {
    "clear_cut": {"p": 0.95},
    "verdict": {
        "answer": "ship",
        "probs": {"ship": 0.6, "request_changes": 0.3, "unclear": 0.1},
        "confidence": 0.6,
    },
}
"""A clear-cut probability over the line, but a verdict the gate is not confident in."""


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


@pytest.fixture(scope="module")
def panel() -> Panel:
    return resolve_panel(allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[RecordingTrace]:
    with RecordingTrace(tmp_path / "traces.duckdb") as opened:
        yield opened


def _run(
    panel: Panel,
    repo: Path,
    trace: Trace,
    jev: FakeJev,
    runner: FakeRunner | None = None,
    **kwargs: Any,
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
        pipeline_run_id=kwargs.pop("pipeline_run_id", RUN_ID),
        **kwargs,
    )
    return result, runner


def _judge_called(runner: FakeRunner) -> bool:
    return any(call["spec"].name == JUDGE[0] for call in runner.calls)


def _gate_rows(trace: RecordingTrace) -> list[TraceRow]:
    return [row for row in trace.rows if row.tag == JUDGE_GATE_TAG]


# --- the panel resolves the gate


def test_the_panel_pins_judge_gate_to_the_0_1_range(panel: Panel) -> None:
    assert panel.gate is not None
    assert panel.gate.address == "judge-gate@0.1.0"
    assert review_panel.JUDGE_GATE == ("judge-gate", "^0.1")


def test_judge_gate_false_leaves_the_gate_out() -> None:
    assert resolve_panel(allow_unpublished=True, judge_gate=False).gate is None


def test_judge_gate_is_not_published_yet() -> None:
    with pytest.raises(NotPublished):
        resolve_judge_gate()


# --- the gate is sure: the judge does not run


def test_a_clear_cut_ship_skips_the_judge(panel: Panel, repo: Path, trace: RecordingTrace) -> None:
    result, runner = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_SHIP))
    assert not _judge_called(runner)
    assert len(runner.calls) == 3
    assert result.judgement is None
    assert result.assessment is None
    assert result.ship is True
    assert result.gate.skipped
    assert result.gate.path == PATH_SKIPPED
    assert result.gate.verdict == "ship"
    assert result.gate.decision == gates.Decision(
        skip=True, escalate=False, route=(), reason="judge_gate"
    )


def test_a_clear_cut_block_skips_the_judge_and_does_not_ship(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, runner = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_BLOCK))
    assert not _judge_called(runner)
    assert result.ship is False
    assert result.gate.verdict == "request_changes"


def test_the_document_has_no_judge_or_score_when_the_judge_was_skipped(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _ = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_SHIP))
    document = json.loads(json.dumps(result.to_json()))
    assert document["judge"] is None
    assert document["weighted_score"] is None
    assert document["ship"] is True
    assert list(document["judge_gate"]) == [
        "status", "ref", "decision", "probabilities", "truncated", "path", "verdict",
        "judge_cost_estimate_usd", "error",
    ]  # fmt: skip
    assert document["judge_gate"]["status"] == "ok"
    assert document["judge_gate"]["ref"] == "judge-gate@0.1.0"
    assert document["judge_gate"]["path"] == PATH_SKIPPED
    assert document["judge_gate"]["probabilities"] == CLEAR_SHIP
    assert document["judge_gate"]["judge_cost_estimate_usd"] > 0


def test_the_decision_row_shows_the_saving(panel: Panel, repo: Path, trace: RecordingTrace) -> None:
    result, _ = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_SHIP))
    (row,) = _gate_rows(trace)
    assert row.kind == "jev"
    assert row.agent_ref == "judge-gate@0.1.0"
    assert row.pipeline_run_id == RUN_ID
    assert row.cost_usd == 0.0
    assert row.error is None
    assert row.output_json == CLEAR_SHIP
    assert row.decision_json == {
        "skip": True,
        "escalate": False,
        "route": [],
        "reason": "judge_gate",
        "path": PATH_SKIPPED,
        "judge_cost_estimate_usd": result.gate.judge_cost_estimate_usd,
        "truncated": False,
    }


def test_the_estimate_is_not_charged_to_the_pipeline(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _ = _run(panel, repo, trace, FakeJev(cost_usd=0.01, gate_answers=CLEAR_SHIP))
    # Three reviewers at 0.25 and two Jev calls at 0.01; the estimate and the unrun judge add nothing.
    assert result.total_cost_usd == pytest.approx(0.77)
    assert trace.cost_by_pipeline_run(RUN_ID) == pytest.approx(0.77)


def test_skipping_the_judge_costs_less_than_running_it(
    panel: Panel, repo: Path, trace: RecordingTrace, tmp_path: Path
) -> None:
    skipped, _ = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_SHIP))
    with RecordingTrace(tmp_path / "other.duckdb") as other:
        ran, _ = _run(panel, repo, other, FakeJev())
    assert skipped.total_cost_usd < ran.total_cost_usd


def test_the_text_report_says_the_judge_was_skipped(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, _ = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_SHIP))
    text = result.text()
    assert "judge gate: skipped the judge, verdict ship, saved about $" in text


# --- the gate is not sure: the judge runs


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        (None, "no_gate"),
        (UNSURE, "unsure"),
        (
            {
                "clear_cut": {"p": 0.95},
                "verdict": {
                    "answer": "unclear",
                    "probs": {"ship": 0.1, "request_changes": 0.1, "unclear": 0.8},
                    "confidence": 0.9,
                },
            },
            "unclear",
        ),
    ],
)
def test_a_gate_that_is_not_sure_lets_the_judge_run(
    panel: Panel, repo: Path, trace: RecordingTrace, answers: dict[str, Any] | None, reason: str
) -> None:
    result, runner = _run(panel, repo, trace, FakeJev(gate_answers=answers))
    assert _judge_called(runner)
    assert result.judgement is not None
    assert result.assessment is not None
    assert result.gate.path == PATH_ESCALATED
    assert result.gate.decision is not None
    assert result.gate.decision.reason == reason
    assert result.gate.verdict is None
    assert result.gate.judge_cost_estimate_usd is None
    assert result.gate.text() == f"judge gate: {reason}, the judge ran"
    (row,) = _gate_rows(trace)
    assert row.decision_json["path"] == PATH_ESCALATED
    assert row.decision_json["judge_cost_estimate_usd"] is None


def test_the_judge_runs_after_the_gate_and_the_gate_after_the_reviewers(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    _run(panel, repo, trace, FakeJev())
    agents = [row.agent_ref.split("@")[0] for row in trace.rows]
    assert agents[:1] == ["diff-triage"]
    gate_at = agents.index("judge-gate", agents.index("diff-triage") + 1)
    assert all(agents.index(name) < gate_at for name in ("code-reviewer", "security-reviewer"))
    assert agents.index("judge") > gate_at


# --- the gate call


def test_the_gate_is_called_with_the_rendered_reviews_under_the_pipeline_run_id(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev()
    _run(panel, repo, trace, jev)
    (call,) = jev.calls_to("judge-gate")
    assert call["spec"].address == "judge-gate@0.1.0"
    assert call["pipeline_run_id"] == RUN_ID
    assert list(call["payload"]) == ["reviews"]
    assert "code-reviewer@1.2.0: " in call["payload"]["reviews"]
    assert "[major] shop/shipping.py:12-12" in call["payload"]["reviews"]


def test_the_gate_is_charged_to_the_panel_budget(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev()
    budget = Budget(3.0)
    _run(panel, repo, trace, jev, budget=budget)
    (call,) = jev.calls_to("judge-gate")
    assert call["budget"] is budget


def test_a_gate_over_budget_ends_the_panel_before_the_judge(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    class OverBudgetOnGate(FakeJev):
        def __call__(self, spec: Any, payload: Any, trace: Any, **kwargs: Any) -> Any:
            if spec.name == "judge-gate":
                raise BudgetExceeded(1.0, 1.2)
            return super().__call__(spec, payload, trace, **kwargs)

    runner = FakeRunner()
    with pytest.raises(BudgetExceeded):
        _run(panel, repo, trace, OverBudgetOnGate(), runner)
    assert not _judge_called(runner)


# --- truncation and failures


def test_a_truncated_gate_input_never_skips(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    result, runner = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_SHIP, truncated=True))
    assert _judge_called(runner)
    assert result.gate.truncated is True
    assert result.gate.decision == gates.Decision(
        skip=False, escalate=True, route=(), reason="truncated"
    )
    (row,) = _gate_rows(trace)
    assert row.decision_json["truncated"] is True


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("no")])
def test_a_jev_failure_escalates_and_the_judge_runs(
    panel: Panel, repo: Path, trace: RecordingTrace, error: Exception
) -> None:
    result, runner = _run(panel, repo, trace, FakeJev(error=error))
    assert result.gate.status == "jev_error"
    assert result.gate.decision == gates.Decision(
        skip=False, escalate=True, route=(), reason="jev_error"
    )
    assert result.gate.probabilities is None
    assert type(error).__name__ in (result.gate.error or "")
    assert _judge_called(runner)
    assert result.gate.path == PATH_ESCALATED
    (row,) = _gate_rows(trace)
    assert row.error == f"{type(error).__name__}: {error}"
    assert row.output_json is None


def test_a_failed_reviewer_escalates_without_calling_jev(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    jev = FakeJev(gate_answers=CLEAR_SHIP)
    runner = FakeRunner(failing={"perf-reviewer": RuntimeError("down")})
    result, _ = _run(panel, repo, trace, jev, runner)
    assert jev.calls_to("judge-gate") == []
    assert result.gate.status == "not_run"
    assert result.gate.decision == gates.Decision(
        skip=False, escalate=True, route=(), reason=REVIEWER_FAILED
    )
    assert _judge_called(runner)
    assert result.judgement is not None
    assert result.gate.text() == f"judge gate: {REVIEWER_FAILED}, the judge ran"


def test_the_gate_never_hides_a_reviewers_failure_in_the_verdict(
    panel: Panel, repo: Path, trace: RecordingTrace
) -> None:
    runner = FakeRunner(failing={"code-reviewer": RuntimeError("down")})
    result, _ = _run(panel, repo, trace, FakeJev(gate_answers=CLEAR_SHIP), runner)
    assert not result.gate.skipped
    assert result.judgement is not None


# --- --no-judge-gate


def test_a_panel_without_the_gate_never_calls_it(repo: Path, trace: RecordingTrace) -> None:
    jev = FakeJev(gate_answers=CLEAR_SHIP)
    no_gate = resolve_panel(allow_unpublished=True, judge_gate=False)
    result, runner = _run(no_gate, repo, trace, jev)
    assert jev.calls_to("judge-gate") == []
    assert _gate_rows(trace) == []
    assert _judge_called(runner)
    assert result.gate is GATE_DISABLED
    assert result.gate.status == "disabled"
    assert result.to_json()["judge_gate"] == {
        "status": "disabled",
        "ref": None,
        "decision": None,
        "probabilities": None,
        "truncated": False,
        "path": None,
        "verdict": None,
        "judge_cost_estimate_usd": None,
        "error": None,
    }
    assert "judge gate: disabled" in result.text()


# --- rendering and estimating


def _result(address: str, review: Any, error: str | None = None) -> ReviewerResult:
    spec = next(s for s in resolve_panel(allow_unpublished=True).reviewers if s.name == address)
    return ReviewerResult(spec, review, error)


def test_render_reviews_has_one_block_per_reviewer_in_order() -> None:
    found = ReviewOutput.model_validate(_review(_finding("a/b.py", 3, 4, "major")))
    clean = ReviewOutput.model_validate(_review())
    text = render_reviews(
        [
            _result("code-reviewer", found),
            _result("security-reviewer", clean),
            _result("perf-reviewer", None, "RuntimeError: down"),
        ]
    )
    blocks = text.split("\n\n")
    assert len(blocks) == 3
    assert blocks[0].splitlines()[0] == "code-reviewer@1.2.0: request_changes"
    assert "[major] a/b.py:3-4 Problem" in blocks[0]
    assert blocks[1].splitlines()[-1] == "no findings"
    assert blocks[2] == "perf-reviewer@0.3.0: FAILED RuntimeError: down"


class _Text(BaseModel):
    text: str


def test_estimate_judge_cost_prices_the_input_and_a_fixed_output(panel: Panel) -> None:
    small = estimate_judge_cost(panel.judge, _Text(text="x" * 4000))
    big = estimate_judge_cost(panel.judge, _Text(text="x" * 40000))
    assert small is not None
    assert big is not None
    assert 0 < small < big


# --- the command line


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Point the CLI at a scratch trace store and fakes; returns them by key."""
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fakes = {"runner": FakeRunner(), "jev": FakeJev(gate_answers=CLEAR_SHIP)}
    monkeypatch.setattr(review_panel, "run", fakes["runner"])
    monkeypatch.setattr(review_panel, "run_jev", fakes["jev"])
    return fakes


def _args(repo: Path, *extra: str) -> list[str]:
    return ["review", str(repo), "--base", "base", "--head", "head", "--allow-unpublished", *extra]


def test_the_parser_knows_no_judge_gate() -> None:
    parser = build_parser()
    base = ["review", "r", "--base", "b", "--head", "h"]
    assert parser.parse_args([*base, "--no-judge-gate"]).no_judge_gate is True
    assert parser.parse_args(base).no_judge_gate is False


def test_review_json_skips_the_judge_by_default_when_the_gate_is_sure(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    assert main(_args(repo, "--json")) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["judge"] is None
    assert document["weighted_score"] is None
    assert document["ship"] is True
    assert document["judge_gate"]["path"] == PATH_SKIPPED
    # The fake triage routes to code and security, so two of the three reviewers run.
    assert len(cli["runner"].calls) == 2


def test_review_text_says_the_judge_was_skipped(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    assert main(_args(repo)) == ExitCode.OK
    output = capsys.readouterr()
    assert "judge gate: skipped the judge, verdict ship" in output.out + output.err


def test_review_no_judge_gate_bypasses_the_gate(
    capsys: pytest.CaptureFixture[str], cli: dict[str, Any], repo: Path
) -> None:
    assert main(_args(repo, "--json", "--no-judge-gate")) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["judge_gate"]["status"] == "disabled"
    assert document["judge"] is not None
    assert cli["jev"].calls_to("judge-gate") == []
    # The fake triage routes to code and security: two reviewers, then the judge.
    assert len(cli["runner"].calls) == 3
