"""The work-item store: tickets, their states and the log of every move (ADR 0002, #82).

Two tables in the trace store's DuckDB file, `work_items` and `work_item_events`, added as
migrations 4 and 5 in `runtime/trace.py`. `WorkItems` borrows the `Trace` instance's connection and
lock through `Trace.connection()`, so a process still holds one connection and one writer, and the
store is safe to share between threads and runs on one event loop exactly as the trace is.

Every change of status goes through one table of legal pairs, `TRANSITIONS`, and writes an event
with a reason in the same transaction as the row's new `status`. Nothing here asks a model
anything: promotion and ordering are code over the store. The rules are in
`docs/CONVENTIONS.md` (Work items) and the commands in `docs/work-items.md`.

The cost so far is never stored. `cost_usd` sums the trace rows of the item's pipeline runs on
every read, so it cannot drift from the trace.
"""

import builtins
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, get_args

import duckdb

from factory.exit import NotFoundError, UsageError
from registry.common_schemas import Ticket, TicketKind
from runtime.trace import Trace

Status = Literal[
    "todo", "ready", "in_progress", "in_review", "blocked", "escalated", "done", "dropped"
]
Size = Literal["S", "M", "L"]

STATUSES: tuple[Status, ...] = get_args(Status)
"""Every state, in the order `docs/CONVENTIONS.md` lists them."""

KINDS: tuple[TicketKind, ...] = get_args(TicketKind)
"""Every kind of work item."""

SIZES: tuple[Size, ...] = get_args(Size)
"""The sizes story-breakdown may set."""

FINAL: frozenset[Status] = frozenset({"done", "dropped"})
"""States nothing moves out of."""

TRANSITIONS: frozenset[tuple[Status | None, Status]] = frozenset(
    {
        (None, "todo"),
        ("todo", "ready"),
        ("todo", "blocked"),
        ("todo", "dropped"),
        ("ready", "in_progress"),
        ("ready", "dropped"),
        ("in_progress", "in_review"),
        ("in_progress", "blocked"),
        ("in_progress", "escalated"),
        ("in_progress", "ready"),
        ("in_review", "done"),
        ("in_review", "ready"),
        ("in_review", "dropped"),
        ("blocked", "todo"),
        ("blocked", "ready"),
        ("blocked", "escalated"),
        ("blocked", "dropped"),
        ("escalated", "ready"),
        ("escalated", "dropped"),
    }
)
"""Every legal `(from, to)` pair; `None` is "no item yet". Every other pair is refused."""

STORE_MOVES: frozenset[tuple[Status, Status]] = frozenset({("todo", "ready"), ("todo", "blocked")})
"""The only moves actor `store` makes: the promotions and blocks of `ready()`."""

EDITABLE: frozenset[Status] = frozenset({"todo", "blocked"})
"""States whose dependencies may still change with `set_depends_on`."""

_ID = re.compile(r"wi-([1-9][0-9]*)")
_PIPELINE_ACTOR = re.compile(r"pipeline:[a-z_][a-z0-9_]*")
_COLUMNS = (
    "id, kind, title, body, acceptance, repo, ref, parent, depends_on, impacted_paths, size,"
    " status, pipeline_run_ids, timezone('UTC', created_at), timezone('UTC', updated_at)"
)


class IllegalTransitionError(UsageError):
    """A move the transition table or the dependency rule refuses (exit 2)."""


class WorkItemNotFound(NotFoundError):
    """No work item has the id asked for (exit 3)."""


class InvalidWorkItem(UsageError):
    """A bad field, actor or dependency, a dependency cycle among them (exit 2)."""


@dataclass(frozen=True)
class Event:
    """One row of `work_item_events`: a change of status, the creation included."""

    work_item_id: str
    at: datetime
    from_status: Status | None
    to_status: Status
    actor: str
    pipeline_run_id: str | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        """Return the event as the JSON object `factory ticket show --json` prints."""
        return {
            "at": _iso(self.at),
            "from_status": self.from_status,
            "to_status": self.to_status,
            "actor": self.actor,
            "pipeline_run_id": self.pipeline_run_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkItem:
    """One row of `work_items`. Lists are tuples so an item read back cannot be edited in place."""

    id: str
    kind: TicketKind
    title: str
    body: str
    acceptance: tuple[str, ...]
    repo: str
    ref: str
    parent: str | None
    depends_on: tuple[str, ...]
    impacted_paths: tuple[str, ...]
    size: Size | None
    status: Status
    pipeline_run_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime

    def ticket(self) -> Ticket:
        """Return the part of the item an agent may see, as the `Ticket` payload model."""
        return Ticket(
            kind=self.kind,
            title=self.title,
            body=self.body,
            acceptance=list(self.acceptance),
            repo=self.repo,
            ref=self.ref,
            impacted_paths=list(self.impacted_paths),
        )

    def to_json(self) -> dict[str, Any]:
        """Return every column as a JSON object, timestamps as ISO 8601 UTC with a `Z`."""
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "acceptance": list(self.acceptance),
            "repo": self.repo,
            "ref": self.ref,
            "parent": self.parent,
            "depends_on": list(self.depends_on),
            "impacted_paths": list(self.impacted_paths),
            "size": self.size,
            "status": self.status,
            "pipeline_run_ids": list(self.pipeline_run_ids),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


@dataclass(frozen=True)
class ReadyResult:
    """What `ready()` did and what is ready now.

    `promoted` and `blocked` are the ids it moved, in id order. `ready` is every item in `ready`
    after the pass, in the orchestrator's order (see `WorkItems.ready`).
    """

    promoted: tuple[str, ...]
    blocked: tuple[str, ...]
    ready: tuple[WorkItem, ...]


class WorkItems:
    """The work-item store over one `Trace`. Opening it opens nothing; the first call does.

    `now` returns the time stamped on rows and events. It must return an aware datetime; tests pass
    a fixed clock.
    """

    def __init__(self, trace: Trace, *, now: Callable[[], datetime] | None = None) -> None:
        """Use `trace`'s file, connection and lock. The caller keeps ownership of `trace`."""
        self.trace = trace
        self._now = now or (lambda: datetime.now(UTC))

    def add(
        self,
        ticket: Ticket,
        *,
        parent: str | None = None,
        depends_on: Sequence[str] = (),
        size: Size | None = None,
        actor: str = "human",
        pipeline_run_id: str | None = None,
        reason: str = "created",
    ) -> WorkItem:
        """Write a new item in `todo` with its creation event, and return it.

        The id is `wi-<n>`, one more than the highest in the store. `parent` is null, an existing
        `wi-<n>` or `prd:<pipeline_run_id>`. Every id in `depends_on` must exist, once. Raises
        `WorkItemNotFound` for an unknown parent or dependency and `InvalidWorkItem` for a bad
        size, parent, actor or reason. A new item has no dependents, so it cannot close a cycle.
        """
        ticket = Ticket.model_validate(ticket.model_dump())
        if size is not None and size not in SIZES:
            raise InvalidWorkItem(f"size {size!r} is not one of {', '.join(SIZES)}")
        _check_actor(actor, pipeline_run_id)
        reason = _check_reason(reason)
        deps = _unique(depends_on)
        with self.trace.connection() as con, _transaction(con):
            if parent is not None:
                if parent.startswith("prd:"):
                    if parent == "prd:":
                        raise InvalidWorkItem("parent prd: needs a pipeline run id")
                else:
                    self._get(con, _check_id(parent))
            for dep in deps:
                self._get(con, _check_id(dep))
            item_id = f"wi-{self._max_number(con) + 1}"
            at = self._stamp()
            con.execute(
                "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    item_id,
                    ticket.kind,
                    ticket.title,
                    ticket.body,
                    json.dumps(ticket.acceptance),
                    ticket.repo,
                    ticket.ref,
                    parent,
                    json.dumps(deps),
                    json.dumps(ticket.impacted_paths),
                    size,
                    "todo",
                    "[]",
                    at,
                    at,
                ],
            )
            self._event(con, item_id, at, None, "todo", actor, pipeline_run_id, reason)
            return self._get(con, item_id)

    def get(self, item_id: str) -> WorkItem:
        """Return the item with `item_id`. Raises `WorkItemNotFound` when there is none."""
        with self.trace.connection() as con:
            return self._get(con, item_id)

    def move(
        self,
        item_id: str,
        to: str,
        reason: str,
        *,
        actor: str = "human",
        pipeline_run_id: str | None = None,
    ) -> WorkItem:
        """Move the item to `to`, write the event with `reason`, and return the moved item.

        Raises `WorkItemNotFound` for an unknown id, `IllegalTransitionError` for a pair not in
        `TRANSITIONS`, a move to the current state, a move out of `done` or `dropped`, or a move
        into `ready` while a dependency is not `done`. Raises `InvalidWorkItem` for an unknown
        state, an empty reason, or an actor that is not `human`, `store` or `pipeline:<module>`
        (agents never move items). A `pipeline:` actor must give its `pipeline_run_id`; `human` and
        `store` must not. `store` makes only the moves of `ready()`.
        """
        if to not in STATUSES:
            raise InvalidWorkItem(f"{to!r} is not a state", hint=f"one of {', '.join(STATUSES)}")
        _check_actor(actor, pipeline_run_id)
        reason = _check_reason(reason)
        with self.trace.connection() as con, _transaction(con):
            item = self._get(con, item_id)
            return self._move(con, item, to, reason, actor, pipeline_run_id)

    def link_run(self, item_id: str, pipeline_run_id: str) -> WorkItem:
        """Append `pipeline_run_id` to the item's `pipeline_run_ids` and return the item.

        Linking a run already linked changes nothing. Linking works in every state, final ones
        included, because a run's cost belongs to the item whatever happened to it. Raises
        `WorkItemNotFound` for an unknown id and `InvalidWorkItem` for an empty run id.
        """
        if not pipeline_run_id.strip():
            raise InvalidWorkItem("pipeline run id must not be empty")
        with self.trace.connection() as con, _transaction(con):
            item = self._get(con, item_id)
            if pipeline_run_id in item.pipeline_run_ids:
                return item
            con.execute(
                "UPDATE work_items SET pipeline_run_ids = ?, updated_at = ? WHERE id = ?",
                [json.dumps([*item.pipeline_run_ids, pipeline_run_id]), self._stamp(), item_id],
            )
            return self._get(con, item_id)

    def set_depends_on(self, item_id: str, depends_on: Sequence[str], reason: str) -> WorkItem:
        """Replace the item's dependencies and return it. Only `todo` and `blocked` items change.

        Every id must exist, once, and the graph must stay acyclic: an item may not depend on
        itself or on anything that depends on it. Raises `WorkItemNotFound` for an unknown id,
        `InvalidWorkItem` for a cycle, a repeat or an empty reason, and `IllegalTransitionError`
        for an item in another state. The status does not change, so no event is written; the
        next `ready()` re-checks the item.
        """
        _check_reason(reason)
        deps = _unique(depends_on)
        with self.trace.connection() as con, _transaction(con):
            item = self._get(con, item_id)
            if item.status not in EDITABLE:
                raise IllegalTransitionError(
                    f"{item_id} is {item.status}; dependencies change only in todo or blocked"
                )
            graph = {i.id: i.depends_on for i in self._all(con)}
            for dep in deps:
                if dep not in graph:
                    raise WorkItemNotFound(f"no work item {dep}")
            graph[item_id] = tuple(deps)
            cycle = _cycle(graph, item_id)
            if cycle:
                raise InvalidWorkItem(f"dependency cycle: {' -> '.join(cycle)}")
            con.execute(
                "UPDATE work_items SET depends_on = ?, updated_at = ? WHERE id = ?",
                [json.dumps(deps), self._stamp(), item_id],
            )
            return self._get(con, item_id)

    def ready(self) -> ReadyResult:
        """Promote every `todo` item whose dependencies are all `done`, and return what is ready.

        In one transaction, in id order: a `todo` item with a `dropped` dependency moves to
        `blocked`, one whose dependencies are all `done` (or that has none) moves to `ready`, both
        with actor `store`; the rest stay. The `ready` items come back in the orchestrator's
        order: `incident` first, then more transitive dependents (dropped items not counted)
        first, then older `created_at`, then the lower id. The Jev ranking key of
        `docs/CONVENTIONS.md` is absent until a ranking agent exists.
        """
        promoted: builtins.list[str] = []
        blocked: builtins.list[str] = []
        with self.trace.connection() as con, _transaction(con):
            items = {i.id: i for i in self._all(con)}
            for item in sorted(items.values(), key=lambda i: _number(i.id)):
                if item.status != "todo":
                    continue
                statuses = {dep: items[dep].status for dep in item.depends_on}
                dropped = [dep for dep, status in statuses.items() if status == "dropped"]
                if dropped:
                    reason = f"dependency {', '.join(dropped)} was dropped"
                    items[item.id] = self._move(con, item, "blocked", reason, "store", None)
                    blocked.append(item.id)
                elif all(status == "done" for status in statuses.values()):
                    reason = "every dependency is done" if statuses else "no dependencies"
                    items[item.id] = self._move(con, item, "ready", reason, "store", None)
                    promoted.append(item.id)
            dependents = _dependents(items.values())
            ready = sorted(
                (i for i in items.values() if i.status == "ready"),
                key=lambda i: (
                    i.kind != "incident",
                    -dependents[i.id],
                    i.created_at,
                    _number(i.id),
                ),
            )
        return ReadyResult(tuple(promoted), tuple(blocked), tuple(ready))

    def events(self, item_id: str) -> builtins.list[Event]:
        """Return the item's events, oldest first. Raises `WorkItemNotFound` for an unknown id."""
        with self.trace.connection() as con:
            self._get(con, item_id)
            found = con.execute(
                "SELECT work_item_id, timezone('UTC', \"at\"), from_status, to_status, actor,"
                " pipeline_run_id, reason FROM work_item_events WHERE work_item_id = ?"
                ' ORDER BY "at", rowid',
                [item_id],
            ).fetchall()
        return [
            Event(wid, at.replace(tzinfo=UTC), frm, to, actor, run, reason)
            for wid, at, frm, to, actor, run, reason in found
        ]

    def cost_usd(self, item_id: str) -> float:
        """Return the cost so far: the sum of `cost_usd` over the trace rows of the item's runs.

        Read from the trace every time, never stored. Unpriced rows add nothing, and an item with
        no linked run costs 0.0. Raises `WorkItemNotFound` for an unknown id.
        """
        with self.trace.connection() as con:
            runs = self._get(con, item_id).pipeline_run_ids
            if not runs:
                return 0.0
            row = con.execute(
                "SELECT COALESCE(sum(cost_usd), 0.0) FROM traces"
                " WHERE list_contains(?::VARCHAR[], pipeline_run_id)",
                [list(runs)],
            ).fetchone()
        return float(row[0]) if row else 0.0

    def document(self, item_id: str) -> dict[str, Any]:
        """Return the item as `factory ticket show --json` prints it: columns, cost and events."""
        with self.trace.connection():
            item = self.get(item_id)
            return {
                **item.to_json(),
                "cost_usd": round(self.cost_usd(item_id), 6),
                "events": [event.to_json() for event in self.events(item_id)],
            }

    def list(self, status: str | None = None) -> builtins.list[WorkItem]:
        """Return every item, or those in `status`, in id order.

        Raises `InvalidWorkItem` for a `status` that is not a state.
        """
        if status is not None and status not in STATUSES:
            raise InvalidWorkItem(
                f"{status!r} is not a state", hint=f"one of {', '.join(STATUSES)}"
            )
        with self.trace.connection() as con:
            items = self._all(con)
        return [i for i in items if status is None or i.status == status]

    def _move(
        self,
        con: duckdb.DuckDBPyConnection,
        item: WorkItem,
        to: Status,
        reason: str,
        actor: str,
        pipeline_run_id: str | None,
    ) -> WorkItem:
        frm = item.status
        if frm == to:
            raise IllegalTransitionError(f"{item.id} is already {to}")
        if frm in FINAL:
            raise IllegalTransitionError(f"{item.id} is {frm}, a final state; nothing moves it")
        if (frm, to) not in TRANSITIONS:
            raise IllegalTransitionError(
                f"{item.id} cannot move from {frm} to {to}", hint=_legal_from(frm)
            )
        if actor == "store" and (frm, to) not in STORE_MOVES:
            raise InvalidWorkItem(f"actor store does not move {frm} to {to}")
        if to == "ready":
            waiting = [dep for dep in item.depends_on if self._get(con, dep).status != "done"]
            if waiting:
                raise IllegalTransitionError(
                    f"{item.id} cannot be ready: {', '.join(waiting)} not done"
                )
        at = self._stamp()
        con.execute(
            "UPDATE work_items SET status = ?, updated_at = ? WHERE id = ?", [to, at, item.id]
        )
        self._event(con, item.id, at, frm, to, actor, pipeline_run_id, reason)
        return self._get(con, item.id)

    def _event(
        self,
        con: duckdb.DuckDBPyConnection,
        item_id: str,
        at: datetime,
        frm: Status | None,
        to: Status,
        actor: str,
        pipeline_run_id: str | None,
        reason: str,
    ) -> None:
        con.execute(
            "INSERT INTO work_item_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            [item_id, at, frm, to, actor, pipeline_run_id, reason],
        )

    def _get(self, con: duckdb.DuckDBPyConnection, item_id: str) -> WorkItem:
        row = con.execute(f"SELECT {_COLUMNS} FROM work_items WHERE id = ?", [item_id]).fetchone()
        if row is None:
            raise WorkItemNotFound(
                f"no work item {item_id}", hint="factory ticket list shows every id"
            )
        return _item(row)

    def _all(self, con: duckdb.DuckDBPyConnection) -> builtins.list[WorkItem]:
        rows = con.execute(f"SELECT {_COLUMNS} FROM work_items").fetchall()
        return sorted((_item(row) for row in rows), key=lambda i: _number(i.id))

    def _max_number(self, con: duckdb.DuckDBPyConnection) -> int:
        ids = con.execute("SELECT id FROM work_items").fetchall()
        return max((_number(i) for (i,) in ids), default=0)

    def _stamp(self) -> datetime:
        at = self._now()
        if at.tzinfo is None:
            raise ValueError("now() must return a timezone-aware datetime")
        return at.astimezone(UTC)


class _transaction:
    """BEGIN on enter, COMMIT on a clean exit and ROLLBACK on any exception."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con

    def __enter__(self) -> None:
        self.con.execute("BEGIN")

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        self.con.execute("COMMIT" if exc_type is None else "ROLLBACK")


def _item(row: tuple[Any, ...]) -> WorkItem:
    (
        item_id,
        kind,
        title,
        body,
        acceptance,
        repo,
        ref,
        parent,
        depends_on,
        impacted_paths,
        size,
        status,
        run_ids,
        created_at,
        updated_at,
    ) = row
    return WorkItem(
        id=item_id,
        kind=kind,
        title=title,
        body=body,
        acceptance=tuple(json.loads(acceptance)),
        repo=repo,
        ref=ref,
        parent=parent,
        depends_on=tuple(json.loads(depends_on)),
        impacted_paths=tuple(json.loads(impacted_paths)),
        size=size,
        status=status,
        pipeline_run_ids=tuple(json.loads(run_ids)),
        created_at=created_at.replace(tzinfo=UTC),
        updated_at=updated_at.replace(tzinfo=UTC),
    )


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(item_id: str) -> int:
    match = _ID.fullmatch(item_id)
    return int(match.group(1)) if match else 0


def _check_id(item_id: str) -> str:
    if not _ID.fullmatch(item_id):
        raise InvalidWorkItem(f"{item_id!r} is not a work item id", hint="ids look like wi-7")
    return item_id


def _check_reason(reason: str) -> str:
    if not reason.strip():
        raise InvalidWorkItem("a reason is required and must not be empty")
    return reason


def _check_actor(actor: str, pipeline_run_id: str | None) -> None:
    if actor in ("human", "store"):
        if pipeline_run_id is not None:
            raise InvalidWorkItem(f"actor {actor} carries no pipeline run id")
        return
    if not _PIPELINE_ACTOR.fullmatch(actor):
        raise InvalidWorkItem(
            f"actor {actor!r} may not move work items",
            hint="actors are human, store or pipeline:<module>; agents never move items",
        )
    if not pipeline_run_id:
        raise InvalidWorkItem(f"actor {actor} must give its pipeline run id")


def _unique(ids: Iterable[str]) -> builtins.list[str]:
    seen: builtins.list[str] = []
    for item_id in ids:
        _check_id(item_id)
        if item_id in seen:
            raise InvalidWorkItem(f"{item_id} is listed twice in depends_on")
        seen.append(item_id)
    return seen


def _legal_from(frm: Status) -> str:
    targets = sorted(to for f, to in TRANSITIONS if f == frm)
    return f"from {frm}: {', '.join(targets)}" if targets else f"nothing moves out of {frm}"


def _cycle(graph: dict[str, tuple[str, ...]], start: str) -> builtins.list[str]:
    """Return a path from `start` back to itself through `graph`, or [] when there is none."""
    path: builtins.list[str] = [start]
    seen: set[str] = set()

    def walk(node: str) -> bool:
        for dep in graph.get(node, ()):
            if dep == start:
                path.append(dep)
                return True
            if dep in seen:
                continue
            seen.add(dep)
            path.append(dep)
            if walk(dep):
                return True
            path.pop()
        return False

    return path if walk(start) else []


def _dependents(items: Iterable[WorkItem]) -> dict[str, int]:
    """Count, for each item, the items that depend on it directly or through others.

    Dropped items depend on nothing any more, so they count for nobody.
    """
    every = list(items)
    live = [i for i in every if i.status != "dropped"]
    graph = {i.id: i.depends_on for i in live}
    counts = {i.id: 0 for i in every}
    for item in live:
        reached: set[str] = set()
        stack = list(graph[item.id])
        while stack:
            dep = stack.pop()
            if dep in reached:
                continue
            reached.add(dep)
            stack.extend(graph.get(dep, ()))
        for dep in reached:
            if dep in counts:
                counts[dep] += 1
    return counts
