"""Tests for runtime/escalations.py: the atomic write, the caps, rendering and the answer rules."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory.exit import ExitCode, exit_code
from registry.common_schemas import Ticket
from runtime import escalations
from runtime.escalations import (
    ANSWER_CHARS,
    ANSWERS_HEADING,
    DIFF_CHARS,
    OUTPUT_CHARS,
    SUITE_TAIL_CHARS,
    TRIGGERS,
    AgentOutput,
    EscalationNotFound,
    InvalidEscalation,
    answer,
    escalate,
    get,
    list_escalations,
    render_markdown,
    ticket_for_run,
    with_answers,
)
from runtime.trace import Trace, TraceRow
from runtime.work_items import IllegalTransitionError, WorkItemNotFound, WorkItems

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
ACTOR = "pipeline:build_loop"
TICKET = Ticket(
    kind="feature",
    title="Add the thing",
    body="Context.",
    acceptance=["the thing exists"],
    repo="agb1986/agb-agent-factory",
    ref="main",
)


class Clock:
    """A clock that moves one minute on every read."""

    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        self.at += timedelta(minutes=1)
        return self.at


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "t.duckdb") as t:
        yield t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(trace: Trace, clock: Clock) -> WorkItems:
    return WorkItems(trace, now=clock)


def in_progress(store: WorkItems, run_id: str = "p1") -> str:
    item = store.add(TICKET)
    store.move(item.id, "ready", "go")
    store.link_run(item.id, run_id)
    store.move(item.id, "in_progress", "start", actor=ACTOR, pipeline_run_id=run_id)
    return item.id


def raise_one(
    trace: Trace, item_id: str, clock: Clock, **changes: object
) -> escalations.Escalation:
    kwargs: dict[str, object] = {
        "trigger": "blocked_output",
        "question": "Which module owns the parser?",
        "reason": "CoderBlockedError: the coder is blocked",
        "actor": ACTOR,
        "pipeline_run_id": "p1",
        "now": clock,
    }
    kwargs.update(changes)
    return escalate(trace, item_id, **kwargs)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_escalate_writes_one_record_and_moves_the_item(
    trace: Trace, store: WorkItems, clock: Clock, trigger: str
) -> None:
    item_id = in_progress(store)
    record = raise_one(
        trace,
        item_id,
        clock,
        trigger=trigger,
        diff="diff --git a/x b/x\n",
        agent_outputs=[AgentOutput.of("feature-coder", "r1", {"blocked": True})],
        suite_tail="1 failed",
    )
    assert record.id == "esc-1"
    assert record.trigger == trigger
    assert record.from_status == "in_progress"
    assert record.pipeline_run_ids == ("p1",)
    assert record.is_open
    assert record.agent_outputs[0].agent == "feature-coder"
    assert '"blocked": true' in record.agent_outputs[0].output
    assert store.get(item_id).status == "escalated"
    last = store.events(item_id)[-1]
    assert (last.to_status, last.actor, last.pipeline_run_id) == ("escalated", ACTOR, "p1")
    assert list_escalations(trace) == [record]


def test_the_cost_is_a_snapshot_of_the_trace_at_the_stop(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    item_id = in_progress(store)
    row = TraceRow(
        run_id="r1", pipeline_run_id="p1", agent_ref="a@1.0.0", kind="llm",
        started_at=T0, duration_ms=1, input_json={}, cost_usd=0.5,
    )  # fmt: skip
    trace.record(row)
    record = raise_one(trace, item_id, clock)
    trace.record(replace(row, run_id="r2"))
    assert record.cost_usd == 0.5
    assert get(trace, record.id).cost_usd == 0.5
    assert store.cost_usd(item_id) == 1.0


def test_a_blocked_item_can_escalate_and_ids_count_up(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    first = in_progress(store)
    raise_one(trace, first, clock)
    second = store.add(TICKET).id
    store.move(second, "blocked", "waiting")
    record = escalate(
        trace, second, trigger="contradiction", question="q", reason="r", actor="human",
        pipeline_run_id=None, now=clock,
    )  # fmt: skip
    assert record.id == "esc-2"
    assert record.from_status == "blocked"


def test_an_illegal_move_writes_no_record(trace: Trace, store: WorkItems, clock: Clock) -> None:
    item_id = store.add(TICKET).id
    with pytest.raises(IllegalTransitionError):
        raise_one(trace, item_id, clock)
    assert list_escalations(trace) == []
    assert store.get(item_id).status == "todo"


def test_an_unknown_item_raises_not_found(trace: Trace, clock: Clock) -> None:
    with pytest.raises(WorkItemNotFound):
        raise_one(trace, "wi-9", clock)


@pytest.mark.parametrize(
    ("changes", "message"),
    [({"trigger": "panic"}, "not a trigger"), ({"question": "  "}, "needs a question")],
)
def test_a_bad_trigger_or_empty_question_is_a_usage_error(
    trace: Trace, store: WorkItems, clock: Clock, changes: dict[str, object], message: str
) -> None:
    item_id = in_progress(store)
    with pytest.raises(InvalidEscalation, match=message) as caught:
        raise_one(trace, item_id, clock, **changes)
    assert exit_code(caught.value) == ExitCode.USAGE
    assert store.get(item_id).status == "in_progress"


def test_large_fields_are_capped_with_a_marker(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    item_id = in_progress(store)
    record = raise_one(
        trace,
        item_id,
        clock,
        diff="d" * (DIFF_CHARS + 10),
        agent_outputs=[AgentOutput("coder", None, "o" * (OUTPUT_CHARS + 5))],
        suite_tail="s" * 10 + "e" * SUITE_TAIL_CHARS,
    )
    assert record.diff.startswith("d" * DIFF_CHARS)
    assert record.diff.endswith(f"[cut: 10 more characters after the first {DIFF_CHARS}]")
    assert record.agent_outputs[0].output.endswith(f"after the first {OUTPUT_CHARS}]")
    assert record.suite_tail.startswith(
        f"[cut: 10 earlier characters before the last {SUITE_TAIL_CHARS}]"
    )
    assert record.suite_tail.endswith("e" * SUITE_TAIL_CHARS)
    assert "s" not in record.suite_tail.splitlines()[1]


def test_render_markdown_puts_the_large_fields_in_details(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    item_id = in_progress(store)
    record = raise_one(
        trace,
        item_id,
        clock,
        diff="+```\n",
        agent_outputs=[AgentOutput.of("feature-coder", "r1", {"blocked": True})],
        suite_tail="FAILED test_x",
    )
    text = render_markdown(record)
    assert text.startswith(f"# esc-1: {item_id} escalated (blocked_output)\n")
    assert "Which module owns the parser?" in text
    assert text.count("<details>") == 3
    assert "<summary>Workspace diff</summary>" in text
    assert "````diff\n+```\n````" in text
    assert "<summary>Output of feature-coder (run `r1`)</summary>" in text
    assert "```json" in text
    assert "Not answered yet." in text
    answered = answer(trace, record.id, "The lexer module.", "cli", now=clock)
    text = render_markdown(answered)
    assert "The lexer module." in text
    assert "From `cli` at" in text


def test_render_markdown_leaves_out_empty_sections(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    record = raise_one(trace, in_progress(store), clock)
    assert "<details>" not in render_markdown(record)


def test_answer_stores_it_and_moves_the_item_to_ready(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    item_id = in_progress(store)
    record = raise_one(trace, item_id, clock)
    answered = answer(trace, record.id, "  Use the lexer.  ", "cli", now=clock)
    assert (answered.answer, answered.answer_source, answered.is_open) == (
        "Use the lexer.",
        "cli",
        False,
    )
    assert answered.answered_at is not None
    assert store.get(item_id).status == "ready"
    last = store.events(item_id)[-1]
    assert (last.from_status, last.to_status, last.actor, last.reason) == (
        "escalated", "ready", "human", "answered esc-1",
    )  # fmt: skip


def test_a_second_answer_is_refused(trace: Trace, store: WorkItems, clock: Clock) -> None:
    record = raise_one(trace, in_progress(store), clock)
    answer(trace, record.id, "first", "cli", now=clock)
    with pytest.raises(InvalidEscalation, match="already answered"):
        answer(trace, record.id, "second", "cli", now=clock)
    assert get(trace, record.id).answer == "first"


def test_an_answer_to_an_item_no_longer_escalated_stores_nothing(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    item_id = in_progress(store)
    record = raise_one(trace, item_id, clock)
    store.move(item_id, "dropped", "not needed")
    with pytest.raises(InvalidEscalation, match="is dropped, not escalated"):
        answer(trace, record.id, "late", "cli", now=clock)
    assert get(trace, record.id).is_open


def test_only_the_last_open_answer_moves_the_item(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    item_id = in_progress(store)
    first = raise_one(trace, item_id, clock)
    store.move(item_id, "ready", "retry by hand without answering")
    store.move(item_id, "in_progress", "again", actor=ACTOR, pipeline_run_id="p1")
    second = raise_one(trace, item_id, clock, trigger="budget", question="Raise the budget?")
    answer(trace, second.id, "Yes, to $5.", "cli", now=clock)
    assert store.get(item_id).status == "escalated"
    assert list_escalations(trace, work_item_id=item_id, open_only=True) == [first]
    answer(trace, first.id, "The lexer.", "file:a.md", now=clock)
    assert store.get(item_id).status == "ready"
    assert list_escalations(trace, open_only=True) == []


@pytest.mark.parametrize(
    ("text", "source", "message"),
    [
        ("  ", "cli", "the answer is empty"),
        ("x" * (ANSWER_CHARS + 1), "cli", "over the limit"),
        ("fine", " ", "needs a source"),
    ],
)
def test_a_bad_answer_is_refused(
    trace: Trace, store: WorkItems, clock: Clock, text: str, source: str, message: str
) -> None:
    record = raise_one(trace, in_progress(store), clock)
    with pytest.raises(InvalidEscalation, match=message):
        answer(trace, record.id, text, source, now=clock)
    assert get(trace, record.id).is_open


@pytest.mark.parametrize("esc_id", ["esc-9", "wi-1", "esc-0"])
def test_an_unknown_record_is_not_found(trace: Trace, esc_id: str) -> None:
    with pytest.raises(EscalationNotFound) as caught:
        get(trace, esc_id)
    assert exit_code(caught.value) == ExitCode.NOT_FOUND
    with pytest.raises(EscalationNotFound):
        answer(trace, esc_id, "x", "cli")


def test_with_answers_appends_answered_records_oldest_first(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    item_id = in_progress(store)
    first = raise_one(trace, item_id, clock, question="First?")
    answer(trace, first.id, "One.", "cli", now=clock)
    store.move(item_id, "in_progress", "again", actor=ACTOR, pipeline_run_id="p1")
    second = raise_one(trace, item_id, clock, question="Second?\nline two")
    assert ticket_for_run(trace, store.get(item_id)).body == (
        f"Context.\n\n{ANSWERS_HEADING}\n\n### esc-1 (blocked_output)\n\nQuestion:\n\n> First?"
        "\n\nAnswer:\n\n> One."
    )
    answer(trace, second.id, "Two.", "cli", now=clock)
    body = ticket_for_run(trace, store.get(item_id)).body
    assert body.index("esc-1") < body.index("esc-2")
    assert "> Second?\n> line two" in body


def test_with_answers_leaves_a_ticket_without_answers_alone(
    trace: Trace, store: WorkItems, clock: Clock
) -> None:
    record = raise_one(trace, in_progress(store), clock)
    assert with_answers(TICKET, [record]) is TICKET
    empty = TICKET.model_copy(update={"body": ""})
    answered = answer(trace, record.id, "Yes.", "cli", now=clock)
    assert with_answers(empty, [answered]).body.startswith(ANSWERS_HEADING)
