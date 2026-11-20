"""Tests for the escalation summary: the input cut, the grounding check, storage and the hook."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gh_fake import FakeGh
from pydantic import BaseModel

from registry.common_schemas import Ticket
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.after_escalation import SUMMARY_AGENT, after_escalation
from runtime.budget import Budget, BudgetExceeded
from runtime.escalation_summary import (
    COST_TOLERANCE_USD,
    EVIDENCE_MIN_CHARS,
    SUMMARY_DIFF_CHARS,
    SUMMARY_OUTPUT_CHARS,
    SUMMARY_SUITE_TAIL_CHARS,
    summary_input,
    ungrounded,
)
from runtime.escalations import (
    AgentOutput,
    Escalation,
    EscalationNotFound,
    InvalidEscalation,
    escalate,
    get_summary,
    render_markdown,
    store_summary,
)
from runtime.outside import OutsideContext
from runtime.trace import Trace
from runtime.work_items import WorkItems
from tools.github import Repository

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
ACTOR = "pipeline:build_loop"
TICKET = Ticket(
    kind="feature",
    title="Add the discount",
    body="Context.",
    acceptance=["carts over 100.00 get 10% off"],
    repo="agb1986/agb-agent-factory",
    ref="main",
)
QUESTION = "Should the discount apply per cart line or to the cart total?"
FINDING = "int(total * 0.9) truncates, so 10001 cents becomes 9000 instead of 9001"
PANEL = json.dumps({"decision": "request_changes", "findings": [{"message": FINDING}]})
GOOD: dict[str, Any] = {
    "paragraph": "wi-1 stopped: the ticket does not say where the discount applies.",
    "options": [
        {
            "label": "Apply it to the cart total",
            "takes": "another_run",
            "cost": "Another coder and panel run with the answer.",
            "evidence": QUESTION,
        },
        {
            "label": "Fix the rounding by hand",
            "takes": "by_hand",
            "cost": "A person's time instead of another run.",
            "evidence": FINDING,
        },
    ],
}


class Clock:
    """A clock that moves one minute on every read."""

    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        self.at += timedelta(minutes=1)
        return self.at


@pytest.fixture(autouse=True)
def no_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    """A fake `gh` with no routes, so a post the notify step tries never leaves the machine."""
    return FakeGh(tmp_path, monkeypatch)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "t.duckdb") as t:
        yield t


@pytest.fixture(scope="module")
def agent() -> LLMSpec:
    name, version_range = SUMMARY_AGENT
    spec = resolve(name, version_range)
    assert isinstance(spec, LLMSpec)
    return spec


def raise_one(trace: Trace, trigger: str = "changes_requested", **changes: Any) -> Escalation:
    clock = Clock()
    items = WorkItems(trace, now=clock)
    item = items.add(TICKET)
    items.move(item.id, "ready", "go")
    items.link_run(item.id, "p1")
    items.move(item.id, "in_progress", "start", actor=ACTOR, pipeline_run_id="p1")
    kwargs: dict[str, Any] = {
        "trigger": trigger,
        "question": QUESTION,
        "reason": "PanelError: changes requested",
        "actor": ACTOR,
        "pipeline_run_id": "p1",
        "now": clock,
        "diff": "+        if total > 10000:\n+            total = int(total * 0.9)\n",
        "agent_outputs": [AgentOutput("review panel", None, PANEL)],
        "suite_tail": "3 passed in 0.02s",
    }
    kwargs.update(changes)
    return escalate(trace, item.id, **kwargs)


def record_json(**changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "question": QUESTION,
        "diff": "",
        "suite_tail": "",
        "agent_outputs": [{"agent": "review panel", "run_id": None, "output": PANEL}],
        "cost_usd": 1.87,
    }
    return record | changes


def with_option(**changes: Any) -> dict[str, Any]:
    option = GOOD["options"][0] | changes
    return {"paragraph": GOOD["paragraph"], "options": [option]}


class Runner:
    """A stand-in for `runtime.runner.run`: records each call and returns `output`."""

    def __init__(self, output: dict[str, Any] | Exception = GOOD, cost: float = 0.002) -> None:
        self.output = output
        self.cost = cost
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        spec: LLMSpec,
        payload: dict[str, Any],
        trace: Trace,
        *,
        pipeline_run_id: str | None,
        run_id: str,
        budget: Budget,
    ) -> BaseModel:
        self.calls.append(
            {
                "spec": spec,
                "payload": payload,
                "pipeline_run_id": pipeline_run_id,
                "run_id": run_id,
                "budget": budget,
            }
        )
        budget.charge(self.cost)
        if isinstance(self.output, Exception):
            raise self.output
        return spec.output_class.model_validate(self.output)


# --- summary_input


def test_summary_input_is_a_valid_agent_input(trace: Trace, agent: LLMSpec) -> None:
    record = raise_one(trace)
    payload = summary_input(record)
    parsed = agent.input_class.model_validate(payload)
    assert parsed.model_dump(mode="json")["agent_outputs"][0]["output"] == PANEL
    assert set(payload) == {
        "id",
        "work_item_id",
        "trigger",
        "question",
        "from_status",
        "cost_usd",
        "diff",
        "agent_outputs",
        "suite_tail",
    }
    assert "actor" not in payload
    assert "answer" not in payload


def test_summary_input_cuts_the_long_fields(trace: Trace) -> None:
    record = raise_one(
        trace,
        diff="d" * (SUMMARY_DIFF_CHARS * 2),
        agent_outputs=[AgentOutput("feature-coder@0.1.0", "r1", "o" * (SUMMARY_OUTPUT_CHARS * 2))],
        suite_tail="x" * (SUMMARY_SUITE_TAIL_CHARS * 2) + "LAST LINE",
    )
    payload = summary_input(record)
    assert len(payload["diff"]) < len(record.diff)
    assert payload["diff"].startswith("d" * 100)
    assert len(payload["agent_outputs"][0]["output"]) < len(record.agent_outputs[0].output)
    assert len(payload["suite_tail"]) < len(record.suite_tail)
    assert payload["suite_tail"].endswith("LAST LINE")


# --- ungrounded


def test_a_summary_quoting_the_record_passes() -> None:
    assert ungrounded(GOOD, record_json()) == []


def test_the_agent_input_is_checked_the_same_as_the_record(trace: Trace) -> None:
    record = raise_one(trace)
    summary = with_option(cost="Another run.")
    assert ungrounded(summary, record.to_json()) == []
    assert ungrounded(summary, summary_input(record)) == []


def test_evidence_not_in_the_record_fails() -> None:
    summary = with_option(evidence="Should the discount apply to every cart?")
    assert ungrounded(summary, record_json()) == [
        "options[0] ('Apply it to the cart total'): evidence is not in the record"
    ]


def test_evidence_joined_from_two_places_fails() -> None:
    joined = "Should the discount apply per cart line int(total * 0.9) truncates"
    assert ungrounded(with_option(evidence=joined), record_json())


def test_short_evidence_fails() -> None:
    short = QUESTION[: EVIDENCE_MIN_CHARS - 1]
    assert ungrounded(with_option(evidence=short), record_json()) == [
        "options[0] ('Apply it to the cart total'): evidence is shorter than 12 characters"
    ]


def test_no_options_fails() -> None:
    assert ungrounded({"paragraph": "p", "options": []}, record_json()) == [
        "the summary names no option"
    ]


def test_whitespace_may_differ_but_case_may_not() -> None:
    spaced = "Should the  discount apply\nper cart line"
    assert ungrounded(with_option(evidence=spaced), record_json()) == []
    assert ungrounded(with_option(evidence=QUESTION.upper()), record_json())


def test_evidence_from_a_decoded_json_value_counts() -> None:
    # The raw output escapes quotes; the decoded message does not.
    message = 'the "flat" discount drops a cent on 10001'
    output = json.dumps({"findings": [{"message": message}]})
    assert message not in output
    record = record_json(agent_outputs=[{"agent": "a", "run_id": None, "output": output}])
    assert ungrounded(with_option(evidence=message), record) == []


def test_evidence_from_the_diff_and_suite_tail_counts() -> None:
    record = record_json(diff="+    total = int(total * 0.9)\n", suite_tail="1 failed, 2 passed")
    assert ungrounded(with_option(evidence="total = int(total * 0.9)"), record) == []
    assert ungrounded(with_option(evidence="1 failed, 2 passed"), record) == []


def test_an_invented_dollar_figure_fails() -> None:
    summary = with_option(cost="Another run, about $3.50 more.")
    assert ungrounded(summary, record_json()) == [
        "options[0].cost: $3.5 is not a figure in the record"
    ]
    paragraph = {"paragraph": "It would cost $40.", "options": GOOD["options"]}
    assert ungrounded(paragraph, record_json()) == ["paragraph: $40 is not a figure in the record"]


def test_the_cost_and_figures_in_the_record_may_be_quoted() -> None:
    near = 1.87 + COST_TOLERANCE_USD / 2
    assert ungrounded(with_option(cost=f"So far ${near:.4f}."), record_json()) == []
    record = record_json(question=QUESTION + " The budget was $5.00.")
    assert ungrounded(with_option(label="Raise the $5.00 budget"), record) == []
    far = 1.87 + COST_TOLERANCE_USD * 4
    assert ungrounded(with_option(cost=f"So far ${far:.3f}."), record_json())


# --- storage and rendering


def test_store_and_get_a_summary(trace: Trace) -> None:
    record = raise_one(trace)
    assert get_summary(trace, record.id) is None
    stored = store_summary(
        trace, record.id, GOOD, run_id="r-sum", agent_ref="escalation-summary@0.1.0"
    )
    assert get_summary(trace, record.id) == stored
    assert stored.to_json() == {
        "escalation_id": record.id,
        "run_id": "r-sum",
        "agent_ref": "escalation-summary@0.1.0",
        "paragraph": GOOD["paragraph"],
        "options": GOOD["options"],
        "created_at": stored.to_json()["created_at"],
    }


def test_a_record_has_at_most_one_summary(trace: Trace) -> None:
    record = raise_one(trace)
    store_summary(trace, record.id, GOOD, run_id="r1", agent_ref="escalation-summary@0.1.0")
    with pytest.raises(InvalidEscalation, match="already has a summary"):
        store_summary(trace, record.id, GOOD, run_id="r2", agent_ref="escalation-summary@0.1.0")
    summary = get_summary(trace, record.id)
    assert summary is not None
    assert summary.run_id == "r1"


@pytest.mark.parametrize(("run_id", "agent_ref"), [("", "a@0.1.0"), ("r1", " ")])
def test_a_summary_needs_its_run_and_agent(trace: Trace, run_id: str, agent_ref: str) -> None:
    record = raise_one(trace)
    with pytest.raises(InvalidEscalation):
        store_summary(trace, record.id, GOOD, run_id=run_id, agent_ref=agent_ref)
    assert get_summary(trace, record.id) is None


def test_a_summary_of_an_unknown_record_is_not_found(trace: Trace) -> None:
    with pytest.raises(EscalationNotFound):
        store_summary(trace, "esc-9", GOOD, run_id="r1", agent_ref="a@0.1.0")
    with pytest.raises(EscalationNotFound):
        get_summary(trace, "esc-9")


def test_render_puts_the_summary_above_the_record(trace: Trace) -> None:
    record = raise_one(trace)
    summary = store_summary(
        trace, record.id, GOOD, run_id="r-sum", agent_ref="escalation-summary@0.1.0"
    )
    text = render_markdown(record, summary)
    assert text.index("## Summary") < text.index("## Question")
    assert GOOD["paragraph"] in text
    assert "1. **Apply it to the cart total** (another_run): Another coder" in text
    assert f"   Evidence: `{QUESTION}`" in text
    assert "Written by `escalation-summary@0.1.0` in run `r-sum`." in text
    assert "## Summary" not in render_markdown(record)


# --- after_escalation


OTHER = "acme/widgets"
"""The hook's outside target: not the ticket's repository, so the notify step is refused before
it reads GitHub. The notify step's own tests are in tests/runtime/test_notify_escalation.py."""


def hook(trace: Trace, record: Escalation, agent: LLMSpec | None, runner: Runner, **kw: Any):
    outside = OutsideContext(
        target=Repository.parse(OTHER), repo_path=Path.cwd(), pipeline_run_id="p1", trace=trace
    )
    options: dict[str, Any] = {
        "budget": Budget(1.0),
        "pipeline_run_id": "p1",
        "dry_run": False,
        "outside": outside,
    }
    options.update(kw)
    return after_escalation(trace, record, summary_agent=agent, runner=runner, **options)


def test_the_summary_is_written_and_stored_with_its_run(trace: Trace, agent: LLMSpec) -> None:
    record = raise_one(trace)
    runner = Runner()
    budget = Budget(1.0)
    after = hook(trace, record, agent, runner, budget=budget)
    assert after.summary.done
    assert after.summary.reason is None
    [call] = runner.calls
    assert call["spec"] is agent
    assert call["payload"] == summary_input(record)
    assert call["pipeline_run_id"] == "p1"
    assert call["budget"] is budget
    assert call["run_id"] == after.summary.run_id
    assert budget.spent == pytest.approx(0.002)
    stored = get_summary(trace, record.id)
    assert stored is not None
    assert (stored.run_id, stored.agent_ref) == (after.summary.run_id, agent.address)
    assert stored.summary == GOOD
    summary = after.to_json()["summary"]
    assert summary == {"done": True, "run_id": call["run_id"], "reason": None}
    assert after.to_json()["notify"]["done"] is False


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"dry_run": True}, "dry run: no summary is written"),
        ({"agent": None}, "no published escalation-summary agent resolved"),
    ],
)
def test_the_summary_is_skipped(
    trace: Trace, agent: LLMSpec, changes: dict[str, Any], reason: str
) -> None:
    record = raise_one(trace)
    runner = Runner()
    use = changes.pop("agent", agent)
    after = hook(trace, record, use, runner, **changes)
    assert not after.summary.done
    assert after.summary.reason == reason
    assert after.summary.run_id is None
    assert runner.calls == []
    assert get_summary(trace, record.id) is None


def test_a_budget_stop_gets_no_summary(trace: Trace, agent: LLMSpec) -> None:
    record = raise_one(trace, trigger="budget")
    runner = Runner()
    after = hook(trace, record, agent, runner)
    assert after.summary.reason == "the run spent its budget, so no summary is written"
    assert runner.calls == []


def test_a_spent_budget_gets_no_summary(trace: Trace, agent: LLMSpec) -> None:
    record = raise_one(trace)
    budget = Budget(0.5)
    budget.charge(0.5)
    with pytest.raises(BudgetExceeded):
        budget.check()
    runner = Runner()
    after = hook(trace, record, agent, runner, budget=budget)
    assert after.summary.reason == "the budget has nothing left for a summary"
    assert runner.calls == []


def test_an_ungrounded_summary_is_not_stored(trace: Trace, agent: LLMSpec) -> None:
    record = raise_one(trace)
    runner = Runner(with_option(evidence="Ship it as it is, the tests pass."))
    after = hook(trace, record, agent, runner)
    assert not after.summary.done
    assert after.summary.run_id == runner.calls[0]["run_id"]
    assert after.summary.reason == (
        "the summary failed its check: options[0] ('Apply it to the cart total'):"
        " evidence is not in the record"
    )
    assert get_summary(trace, record.id) is None


@pytest.mark.parametrize("error", [RuntimeError("the API is down"), BudgetExceeded(1.0, 1.2)])
def test_an_agent_failure_leaves_the_record_as_it_is(
    trace: Trace, agent: LLMSpec, error: Exception
) -> None:
    record = raise_one(trace)
    runner = Runner(error)
    after = hook(trace, record, agent, runner)
    assert not after.summary.done
    assert after.summary.run_id == runner.calls[0]["run_id"]
    assert after.summary.reason is not None
    assert after.summary.reason.startswith(f"{type(error).__name__}: ")
    assert get_summary(trace, record.id) is None
    items = WorkItems(trace)
    assert items.get(record.work_item_id).status == "escalated"


def test_a_failed_store_is_reported_not_raised(trace: Trace, agent: LLMSpec) -> None:
    record = raise_one(trace)
    store_summary(trace, record.id, GOOD, run_id="r0", agent_ref=agent.address)
    after = hook(trace, record, agent, Runner())
    assert not after.summary.done
    assert after.summary.reason is not None
    assert "already has a summary" in after.summary.reason
