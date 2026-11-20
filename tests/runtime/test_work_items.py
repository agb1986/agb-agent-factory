"""Tests for runtime/work_items.py: the transition table, dependencies, `ready()` and the upgrade."""

import itertools
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.exit import ExitCode, exit_code
from registry.common_schemas import Ticket
from runtime.trace import MIGRATIONS, Trace, TraceRow
from runtime.work_items import (
    STATUSES,
    TRANSITIONS,
    IllegalTransitionError,
    InvalidWorkItem,
    NewItem,
    WorkItemNotFound,
    WorkItems,
)

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
TICKET = Ticket(
    kind="feature",
    title="Add the thing",
    body="Context.",
    acceptance=["the thing exists"],
    repo="agb1986/agb-agent-factory",
    ref="main",
)

# A legal path from `todo` to every state, so any pair can start from its `from` state.
PATHS: dict[str, list[str]] = {
    "todo": [],
    "ready": ["ready"],
    "in_progress": ["ready", "in_progress"],
    "in_review": ["ready", "in_progress", "in_review"],
    "blocked": ["blocked"],
    "escalated": ["blocked", "escalated"],
    "done": ["ready", "in_progress", "in_review", "done"],
    "dropped": ["dropped"],
}
PAIRS = list(itertools.product(STATUSES, STATUSES))
LEGAL = [pair for pair in PAIRS if pair in TRANSITIONS]
ILLEGAL = [pair for pair in PAIRS if pair not in TRANSITIONS]


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
def store(trace: Trace) -> WorkItems:
    return WorkItems(trace, now=Clock())


def ticket(**changes: Any) -> Ticket:
    return TICKET.model_copy(update=changes)


def walk(store: WorkItems, item_id: str, to: str) -> None:
    for step in PATHS[to]:
        store.move(item_id, step, f"to {step}")


# --- transitions


def test_the_table_covers_every_state_and_the_final_states_lead_nowhere() -> None:
    assert {to for _, to in TRANSITIONS} | {f for f, _ in TRANSITIONS if f} == set(STATUSES)
    assert not [pair for pair in TRANSITIONS if pair[0] in ("done", "dropped")]
    assert len(LEGAL) == len(TRANSITIONS) - 1  # every pair but the creation one


@pytest.mark.parametrize(("frm", "to"), LEGAL, ids=[f"{f}->{t}" for f, t in LEGAL])
def test_every_legal_move_is_made_and_logged(store: WorkItems, frm: str, to: str) -> None:
    item = store.add(TICKET)
    walk(store, item.id, frm)
    moved = store.move(item.id, to, "because")
    assert moved.status == to
    last = store.events(item.id)[-1]
    assert (last.from_status, last.to_status, last.actor, last.reason) == (
        frm,
        to,
        "human",
        "because",
    )
    assert moved.updated_at == last.at


@pytest.mark.parametrize(("frm", "to"), ILLEGAL, ids=[f"{f}->{t}" for f, t in ILLEGAL])
def test_every_illegal_move_is_refused_with_exit_2(store: WorkItems, frm: str, to: str) -> None:
    item = store.add(TICKET)
    walk(store, item.id, frm)
    before = store.events(item.id)
    with pytest.raises(IllegalTransitionError) as caught:
        store.move(item.id, to, "because")
    assert exit_code(caught.value) == ExitCode.USAGE
    assert store.get(item.id).status == frm
    assert store.events(item.id) == before


def test_a_refused_move_names_the_legal_ones(store: WorkItems) -> None:
    item = store.add(TICKET)
    with pytest.raises(IllegalTransitionError) as caught:
        store.move(item.id, "done", "skip ahead")
    assert caught.value.hint == "from todo: blocked, dropped, ready"


def test_an_unknown_state_an_empty_reason_or_an_unknown_id_is_refused(store: WorkItems) -> None:
    item = store.add(TICKET)
    with pytest.raises(InvalidWorkItem):
        store.move(item.id, "shipped", "x")
    with pytest.raises(InvalidWorkItem):
        store.move(item.id, "ready", "  ")
    with pytest.raises(WorkItemNotFound) as caught:
        store.move("wi-99", "ready", "x")
    assert exit_code(caught.value) == ExitCode.NOT_FOUND
    with pytest.raises(WorkItemNotFound):
        store.get("wi-99")
    with pytest.raises(InvalidWorkItem):
        store.list("shipped")


@pytest.mark.parametrize(
    ("actor", "run_id"),
    [("code-reviewer@1.2.0", None), ("agent", "p1"), ("pipeline:fix_loop", None),
     ("human", "p1"), ("store", "p1"), ("pipeline:Bad", "p1")],
)  # fmt: skip
def test_agents_never_move_items_and_actors_carry_the_right_run_id(
    store: WorkItems, actor: str, run_id: str | None
) -> None:
    item = store.add(TICKET)
    with pytest.raises(InvalidWorkItem):
        store.move(item.id, "ready", "x", actor=actor, pipeline_run_id=run_id)
    assert store.get(item.id).status == "todo"


def test_a_pipeline_actor_moves_with_its_run_id(store: WorkItems) -> None:
    item = store.add(TICKET)
    store.move(item.id, "ready", "picked", actor="pipeline:feature_loop", pipeline_run_id="p1")
    last = store.events(item.id)[-1]
    assert (last.actor, last.pipeline_run_id) == ("pipeline:feature_loop", "p1")


def test_the_store_actor_makes_only_the_moves_of_ready(store: WorkItems) -> None:
    item = store.add(TICKET)
    with pytest.raises(InvalidWorkItem):
        store.move(item.id, "dropped", "x", actor="store")
    store.move(item.id, "blocked", "x", actor="store")


def test_an_item_with_an_undone_dependency_cannot_be_moved_to_ready(store: WorkItems) -> None:
    dep = store.add(TICKET)
    item = store.add(TICKET, depends_on=[dep.id])
    with pytest.raises(IllegalTransitionError):
        store.move(item.id, "ready", "x")
    walk(store, dep.id, "done")
    assert store.move(item.id, "ready", "x").status == "ready"


# --- add and the fields


def test_add_numbers_items_and_writes_the_creation_event(store: WorkItems) -> None:
    first = store.add(TICKET)
    second = store.add(ticket(kind="bug"), parent=first.id, depends_on=[first.id], size="M")
    third = store.add(TICKET, parent="prd:run-1", reason="from story-breakdown")
    assert [first.id, second.id, third.id] == ["wi-1", "wi-2", "wi-3"]
    assert (second.parent, second.depends_on, second.size) == ("wi-1", ("wi-1",), "M")
    assert second.ticket() == ticket(kind="bug")
    assert first.created_at == first.updated_at and first.created_at.tzinfo is UTC
    (event,) = store.events(third.id)
    assert (event.from_status, event.to_status, event.reason) == (
        None,
        "todo",
        "from story-breakdown",
    )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"parent": "wi-7"}, WorkItemNotFound),
        ({"parent": "prd:"}, InvalidWorkItem),
        ({"parent": "nope"}, InvalidWorkItem),
        ({"depends_on": ["wi-7"]}, WorkItemNotFound),
        ({"depends_on": ["wi-1", "wi-1"]}, InvalidWorkItem),
        ({"depends_on": ["7"]}, InvalidWorkItem),
        ({"size": "XL"}, InvalidWorkItem),
        ({"reason": ""}, InvalidWorkItem),
        ({"actor": "code-reviewer"}, InvalidWorkItem),
    ],
)
def test_add_refuses_a_bad_link_size_reason_or_actor(
    store: WorkItems, kwargs: dict[str, Any], error: type[Exception]
) -> None:
    store.add(TICKET)
    with pytest.raises(error):
        store.add(TICKET, **kwargs)
    assert [i.id for i in store.list()] == ["wi-1"]


def test_add_revalidates_a_ticket_built_without_validation(store: WorkItems) -> None:
    with pytest.raises(ValueError):
        store.add(TICKET.model_copy(update={"title": ""}))
    assert store.list() == []


def test_list_filters_by_status_in_id_order(store: WorkItems) -> None:
    for _ in range(11):
        store.add(TICKET)
    store.move("wi-10", "dropped", "dup")
    assert [i.id for i in store.list()][-3:] == ["wi-9", "wi-10", "wi-11"]
    assert [i.id for i in store.list("dropped")] == ["wi-10"]


def test_link_run_appends_once_and_works_in_a_final_state(store: WorkItems) -> None:
    item = store.add(TICKET)
    store.move(item.id, "dropped", "no longer needed")
    store.link_run(item.id, "p1")
    store.link_run(item.id, "p2")
    assert store.link_run(item.id, "p1").pipeline_run_ids == ("p1", "p2")
    with pytest.raises(InvalidWorkItem):
        store.link_run(item.id, " ")
    with pytest.raises(WorkItemNotFound):
        store.link_run("wi-9", "p1")


# --- dependencies


def test_a_dependency_cycle_is_refused(store: WorkItems) -> None:
    a = store.add(TICKET)
    b = store.add(TICKET, depends_on=[a.id])
    c = store.add(TICKET, depends_on=[b.id])
    with pytest.raises(InvalidWorkItem, match="wi-1 -> wi-3 -> wi-2 -> wi-1") as caught:
        store.set_depends_on(a.id, [c.id], "a needs c")
    assert exit_code(caught.value) == ExitCode.USAGE
    with pytest.raises(InvalidWorkItem, match="cycle"):
        store.set_depends_on(a.id, [a.id], "a needs itself")
    assert store.get(a.id).depends_on == ()


def test_set_depends_on_changes_only_todo_and_blocked_items(store: WorkItems) -> None:
    a = store.add(TICKET)
    b = store.add(TICKET)
    c = store.add(TICKET)
    assert store.set_depends_on(c.id, [a.id, b.id], "needs both").depends_on == ("wi-1", "wi-2")
    store.move(c.id, "blocked", "waiting")
    assert store.set_depends_on(c.id, [a.id], "only a").depends_on == ("wi-1",)
    store.move(a.id, "ready", "go")
    with pytest.raises(IllegalTransitionError):
        store.set_depends_on(a.id, [b.id], "too late")
    with pytest.raises(WorkItemNotFound):
        store.set_depends_on(b.id, ["wi-9"], "x")
    with pytest.raises(InvalidWorkItem):
        store.set_depends_on(b.id, [], "")


# --- ready()


def test_ready_promotes_blocks_and_logs_with_actor_store(store: WorkItems) -> None:
    free = store.add(TICKET)
    dep = store.add(TICKET)
    waiting = store.add(TICKET, depends_on=[dep.id])
    gone = store.add(TICKET)
    orphan = store.add(TICKET, depends_on=[gone.id])
    store.move(gone.id, "dropped", "not needed")
    result = store.ready()
    assert result.promoted == (free.id, dep.id)
    assert result.blocked == (orphan.id,)
    assert store.get(waiting.id).status == "todo"
    promoted = store.events(free.id)[-1]
    assert (promoted.actor, promoted.reason, promoted.pipeline_run_id) == (
        "store",
        "no dependencies",
        None,
    )
    assert store.events(orphan.id)[-1].reason == "dependency wi-4 was dropped"
    for step in ("in_progress", "in_review", "done"):
        store.move(dep.id, step, "work")
    again = store.ready()
    assert again.promoted == (waiting.id,)
    assert store.events(waiting.id)[-1].reason == "every dependency is done"
    assert store.ready().promoted == ()


def test_ready_orders_incidents_then_dependents_then_age_then_id(
    trace: Trace, tmp_path: Path
) -> None:
    clock = Clock()
    store = WorkItems(trace, now=clock)
    old = store.add(TICKET)  # wi-1: oldest, no dependents
    hub = store.add(TICKET)  # wi-2: two live dependents
    one = store.add(TICKET)  # wi-3: one live dependent and one dropped
    incident = store.add(ticket(kind="incident"))  # wi-4: newest but an incident
    store.add(TICKET, depends_on=[hub.id])  # wi-5
    store.add(TICKET, depends_on=["wi-5"])  # wi-6, so wi-2 has two transitive dependents
    store.add(TICKET, depends_on=[one.id])  # wi-7
    store.add(TICKET, depends_on=[one.id])  # wi-8, to be dropped
    store.move("wi-8", "dropped", "dup")
    result = store.ready()
    assert [i.id for i in result.ready] == [incident.id, hub.id, one.id, old.id]


def test_ready_breaks_a_created_at_tie_by_id(trace: Trace) -> None:
    store = WorkItems(trace, now=lambda: T0)
    for _ in range(3):
        store.add(TICKET)
    assert [i.id for i in store.ready().ready] == ["wi-1", "wi-2", "wi-3"]


def test_ready_lists_items_already_ready_and_leaves_others(store: WorkItems) -> None:
    a = store.add(TICKET)
    b = store.add(TICKET)
    store.move(a.id, "ready", "by hand")
    walk(store, b.id, "in_progress")
    result = store.ready()
    assert result.promoted == ()
    assert [i.id for i in result.ready] == [a.id]


# --- cost


def test_cost_is_read_from_the_trace_rows_of_the_linked_runs(
    trace: Trace, store: WorkItems
) -> None:
    base = TraceRow(
        run_id="", agent_ref="feature-coder@0.1.0", kind="llm", started_at=T0, duration_ms=1,
        input_json={},
    )  # fmt: skip
    for run_id, pipeline, cost in [
        ("r1", "p1", 0.25),
        ("r2", "p1", None),
        ("r3", "p2", 0.5),
        ("r4", "other", 9.0),
        ("r5", None, 9.0),
    ]:
        trace.record(replace(base, run_id=run_id, pipeline_run_id=pipeline, cost_usd=cost))
    item = store.add(TICKET)
    assert store.cost_usd(item.id) == 0.0
    store.link_run(item.id, "p1")
    assert store.cost_usd(item.id) == 0.25
    store.link_run(item.id, "p2")
    trace.record(replace(base, run_id="r6", pipeline_run_id="p2", cost_usd=0.125))
    assert store.cost_usd(item.id) == 0.875
    assert store.document(item.id)["cost_usd"] == 0.875
    with pytest.raises(WorkItemNotFound):
        store.cost_usd("wi-9")


# --- the store's file


def test_a_store_on_the_older_migrations_upgrades_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.duckdb"
    with Trace(path, migrations=MIGRATIONS[:3]) as old:
        old.record(
            TraceRow(
                run_id="r1", pipeline_run_id="p1", agent_ref="a@1.0.0", kind="llm",
                started_at=T0, duration_ms=1, input_json={}, cost_usd=0.5,
            )
        )  # fmt: skip
        assert old.schema_version() == 3
        tables = old._connect().execute("SELECT table_name FROM information_schema.tables")
        assert "work_items" not in {name for (name,) in tables.fetchall()}
    with Trace(path) as new:
        store = WorkItems(new, now=Clock())
        item = store.add(TICKET)
        store.link_run(item.id, "p1")
        assert new.schema_version() == len(MIGRATIONS) == 5
        assert store.cost_usd(item.id) == 0.5
    with Trace(path) as reopened:
        assert WorkItems(reopened).get("wi-1").pipeline_run_ids == ("p1",)


def test_the_event_table_refuses_an_empty_reason(trace: Trace, store: WorkItems) -> None:
    store.add(TICKET)
    with pytest.raises(Exception, match="CHECK"), trace.connection() as con:
        con.execute(
            "INSERT INTO work_item_events VALUES ('wi-1', ?, 'todo', 'ready', 'human', NULL, '')",
            [T0],
        )


def test_a_failed_move_rolls_back_the_row_and_the_event(
    store: WorkItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = store.add(TICKET)

    def broken(*args: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_event", broken)
    with pytest.raises(RuntimeError):
        store.move(item.id, "ready", "x")
    monkeypatch.undo()
    assert store.get(item.id).status == "todo"
    assert len(store.events(item.id)) == 1


def test_a_naive_clock_is_refused(trace: Trace) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        WorkItems(trace, now=lambda: datetime(2026, 11, 20)).add(TICKET)


def test_add_many_writes_every_item_with_ids_dependencies_and_events(store: WorkItems) -> None:
    store.add(TICKET)
    items = store.add_many(
        [
            NewItem(ticket(title="one"), "S"),
            NewItem(ticket(title="two"), "M", (0,)),
            NewItem(ticket(title="three"), None, (0, 1)),
        ],
        parent="prd:run-1",
        actor="pipeline:plan_loop",
        pipeline_run_id="run-1",
        reason="planned",
    )
    assert [i.id for i in items] == ["wi-2", "wi-3", "wi-4"]
    assert [i.depends_on for i in items] == [(), ("wi-2",), ("wi-2", "wi-3")]
    assert [i.size for i in items] == ["S", "M", None]
    assert {i.parent for i in items} == {"prd:run-1"}
    assert {i.status for i in items} == {"todo"}
    for item in items:
        (event,) = store.events(item.id)
        assert (event.to_status, event.actor, event.pipeline_run_id, event.reason) == (
            "todo",
            "pipeline:plan_loop",
            "run-1",
            "planned",
        )


def test_add_many_may_name_a_later_position_when_the_graph_is_acyclic(store: WorkItems) -> None:
    first, second = store.add_many([NewItem(TICKET, None, (1,)), NewItem(TICKET)])
    assert first.depends_on == (second.id,)


@pytest.mark.parametrize(
    ("batch", "match"),
    [
        ([], "at least one"),
        ([NewItem(TICKET, "XL")], "size"),  # ty: ignore[invalid-argument-type]
        ([NewItem(TICKET, None, (3,))], "not in the batch"),
        ([NewItem(TICKET, None, (-1,))], "not in the batch"),
        ([NewItem(TICKET, None, (0,))], "itself"),
        ([NewItem(TICKET), NewItem(TICKET, None, (0, 0))], "twice"),
        ([NewItem(TICKET, None, (1,)), NewItem(TICKET, None, (0,))], "cycle"),
    ],
)
def test_add_many_refuses_a_bad_batch_and_writes_nothing(
    store: WorkItems, batch: list[NewItem], match: str
) -> None:
    with pytest.raises(InvalidWorkItem, match=match):
        store.add_many(batch)
    assert store.list() == []


def test_add_many_refuses_a_bad_parent_actor_or_reason_and_writes_nothing(
    store: WorkItems,
) -> None:
    with pytest.raises(WorkItemNotFound):
        store.add_many([NewItem(TICKET)], parent="wi-9")
    with pytest.raises(InvalidWorkItem, match="needs a pipeline run id"):
        store.add_many([NewItem(TICKET)], parent="prd:")
    with pytest.raises(InvalidWorkItem, match="pipeline run id"):
        store.add_many([NewItem(TICKET)], actor="pipeline:plan_loop")
    with pytest.raises(InvalidWorkItem, match="reason"):
        store.add_many([NewItem(TICKET)], reason=" ")
    assert store.list() == []


def test_a_failure_halfway_through_add_many_rolls_every_item_back(
    store: WorkItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add(TICKET)
    real = store._event
    calls: list[str] = []

    def flaky(*args: Any) -> None:
        calls.append(args[1])
        if len(calls) == 3:
            raise RuntimeError("disk full")
        real(*args)

    monkeypatch.setattr(store, "_event", flaky)
    with pytest.raises(RuntimeError):
        store.add_many([NewItem(TICKET), NewItem(TICKET), NewItem(TICKET)])
    monkeypatch.undo()
    assert [i.id for i in store.list()] == ["wi-1"]
    assert store.add(TICKET).id == "wi-2"
