"""Factory metrics over the trace store and the work-item store, as read-only queries.

`scripts/factory_report.py` prints these and the future `agb factory` wrapper (#30) reuses them, so
the queries live here and the rendering does not. Every function takes an open DuckDB connection to
the trace store and a window `[start, end)` of timezone-aware datetimes, and only reads: none of
them writes, migrates or commits, so a connection opened with `read_only=True` works. Open one with
`open_store`. Nothing here reads a prompt, a diff or a finding's text, only counts, scores, costs and
timestamps.

What each measure reads, and where it is approximate:

- Tickets per state, and time in each state, replay the `work_item_events` table. A ticket's state
  at a moment is the `to_status` of its last event at or before it, so the report is right for any
  past window and does not depend on `work_items.status`. `done` and `dropped` are end states and
  take no time.
- Cost is `traces.cost_usd` summed over the rows whose `pipeline_run_id` is one of the ticket's
  `pipeline_run_ids`, started in the window, the same sum as `WorkItems.cost_usd` but limited to the
  window. A lifecycle phase is the state the ticket was in when the row started. A run linked to
  two tickets counts for both. Unpriced rows add nothing.
- The panel ship rate follows `factory review`: a run that has a `judge` output ships when
  `runtime.scoring` says its weighted score reaches the threshold of `ScoringSpec`; a run whose
  judge-gate skipped the judge ships when the gate's verdict is `ship`.
- Escalations are the moves into `escalated` in `work_item_events`. #93 adds escalation records
  and may refine this count, for example to separate a question answered from one still open.
- Rubric scores are the `score` of the `eval-rubric` grader rows, averaged per UTC day.
- Cost per agent version groups every trace row by its `agent_ref`, `name@version`. Agents are not
  grouped by team: the `team` field is not in the registry on the integration branch yet (see
  "Teams" in docs/CONVENTIONS.md), so the report leaves it out until it exists.
"""

import contextlib
import datetime
import json
from collections import defaultdict
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from factory.exit import FactoryError, NotFoundError
from registry.spec import ScoringSpec
from runtime.outside import TAG_PREFIX as OUTSIDE_TAG_PREFIX
from runtime.scoring import score_values, ship, weighted_score
from runtime.work_items import STATUSES

type History = dict[str, list[tuple[datetime.datetime, str]]]
"""Each ticket's `(time, state entered)` list, oldest first, replayed from `work_item_events`."""

END_STATES = ("done", "dropped")
"""Work-item states a ticket rests in for good; it spends no time "in" them."""
JUDGE_AGENT = "judge"
"""Name of the panel's judge agent; its rows carry the weighted-score verdict."""
JUDGE_GATE_TAG = "judge_gate:decision"
"""Trace tag of the judge-gate row; equal to `pipelines.review_panel.JUDGE_GATE_TAG` (tested)."""
PATH_SKIPPED = "skipped_judge"
"""Value of `decision_json.path` when the gate skipped the judge."""
RUBRIC_AGENT = "eval-rubric"
"""Name of the rubric grader whose rows carry the rubric scores."""
UNATTRIBUTED = "unattributed"
"""Phase of a cost row that started before the ticket's first event."""

_EVENTS = """
    SELECT work_item_id, timezone('UTC', "at"), from_status, to_status
    FROM work_item_events
    ORDER BY work_item_id, "at", rowid
"""
_RUNS = """
    SELECT id, pipeline_run_ids FROM work_items ORDER BY id
"""
_ROWS = """
    SELECT pipeline_run_id, timezone('UTC', started_at), COALESCE(cost_usd, 0.0)
    FROM traces
    WHERE list_contains(?::VARCHAR[], pipeline_run_id) AND started_at >= ? AND started_at < ?
"""


@contextlib.contextmanager
def open_store(path: Path) -> Generator[duckdb.DuckDBPyConnection]:
    """Open the trace store at `path` read-only, with timestamps read in UTC, for the block.

    The connection is closed at the end. Raises `NotFoundError` when `path` does not exist or is
    not a store with the `traces` table, and `FactoryError` when another process holds the file.
    """
    if not path.exists():
        raise NotFoundError(
            f"no trace store at {path}", hint="pass --trace or set FACTORY_TRACE_DB"
        )
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        raise FactoryError(
            f"could not open {path}: {str(exc).splitlines()[0]}",
            hint="a process writing to the store holds it; run again when it is done",
        ) from exc
    try:
        con.execute("SET TimeZone = 'UTC'")
        yield con
    except duckdb.CatalogException as exc:
        raise NotFoundError(
            f"{path} lacks a table the report reads: {str(exc).splitlines()[0]}",
            hint="the store predates the work-item tables; any `factory ticket` command migrates it",
        ) from exc
    finally:
        con.close()


@dataclass(frozen=True, slots=True)
class StateTime:
    """Time the tickets spent in one state in the window, summed over tickets."""

    state: str
    seconds: float
    tickets: int

    def to_json(self) -> dict[str, Any]:
        """Return the entry as a JSON-ready dict."""
        return {"state": self.state, "seconds": self.seconds, "tickets": self.tickets}


@dataclass(frozen=True, slots=True)
class Escalations:
    """Moves into `blocked` and `escalated` in the window, and how many tickets saw any event.

    `per_ticket` is `escalated / tickets`, `None` when no ticket had an event in the window.
    """

    escalated: int
    blocked: int
    tickets: int

    @property
    def per_ticket(self) -> float | None:
        """Escalations per ticket active in the window, or `None` without a ticket."""
        return self.escalated / self.tickets if self.tickets else None

    def to_json(self) -> dict[str, Any]:
        """Return the counts and the ratio as a JSON-ready dict."""
        return {
            "escalated": self.escalated,
            "blocked": self.blocked,
            "tickets": self.tickets,
            "per_ticket": self.per_ticket,
        }


@dataclass(frozen=True, slots=True)
class TicketCosts:
    """What the tickets' runs cost in the window: in total, per ticket and per lifecycle phase.

    `per_ticket` maps ticket id to dollars for tickets with at least one run in the window, and
    `per_phase` maps a state, or `UNATTRIBUTED`, to dollars. Both sum to `total_usd` unless a run is
    linked to several tickets, which counts once for each.
    """

    total_usd: float
    per_ticket: dict[str, float]
    per_phase: dict[str, float]

    @property
    def mean_per_ticket_usd(self) -> float | None:
        """Mean cost over the tickets that have a run in the window, or `None` without one."""
        return self.total_usd / len(self.per_ticket) if self.per_ticket else None

    def to_json(self) -> dict[str, Any]:
        """Return the costs as a JSON-ready dict."""
        return {
            "total_usd": self.total_usd,
            "mean_per_ticket_usd": self.mean_per_ticket_usd,
            "per_ticket": self.per_ticket,
            "per_phase": self.per_phase,
        }


@dataclass(frozen=True, slots=True)
class PanelShip:
    """How the review panel's runs in the window came out.

    `runs` counts pipeline runs with a judge verdict, `judged` of them from a judge's weighted
    score and `skipped` from the judge-gate's verdict. `shipped` of them ship; `unreadable` judge
    outputs without the four scores are left out of all three. `mean_score` averages the weighted
    scores of the judged runs.
    """

    runs: int
    judged: int
    skipped: int
    shipped: int
    unreadable: int
    mean_score: float | None

    @property
    def ship_rate(self) -> float | None:
        """Shipped runs over runs, or `None` when no run has a verdict."""
        return self.shipped / self.runs if self.runs else None

    def to_json(self) -> dict[str, Any]:
        """Return the counts and the rate as a JSON-ready dict."""
        return {
            "runs": self.runs,
            "judged": self.judged,
            "skipped": self.skipped,
            "shipped": self.shipped,
            "unreadable": self.unreadable,
            "ship_rate": self.ship_rate,
            "mean_score": self.mean_score,
        }


@dataclass(frozen=True, slots=True)
class RubricDay:
    """The rubric grader's scores on one UTC day: how many, their mean and how many passed."""

    day: datetime.date
    scores: int
    mean_score: float
    passed: int

    def to_json(self) -> dict[str, Any]:
        """Return the day as a JSON-ready dict."""
        return {
            "day": self.day.isoformat(),
            "scores": self.scores,
            "mean_score": self.mean_score,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class AgentCost:
    """Runs, errors and cost of one agent version (`agent_ref` is `name@version`) in the window."""

    agent: str
    version: str
    runs: int
    errors: int
    cost_usd: float

    def to_json(self) -> dict[str, Any]:
        """Return the entry as a JSON-ready dict."""
        return {
            "agent": self.agent,
            "version": self.version,
            "runs": self.runs,
            "errors": self.errors,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True, slots=True)
class FactoryReport:
    """Every measure for one window `[start, end)`.

    `tickets` is how many tickets were in each state at `end`, every state listed. `empty` says the
    window holds no trace row and no work-item event: the report then has nothing to show, and a
    caller must say so rather than print its zeros as a result.
    """

    start: datetime.datetime
    end: datetime.datetime
    trace_rows: int
    events: int
    tickets: dict[str, int]
    time_in_state: tuple[StateTime, ...]
    escalations: Escalations
    costs: TicketCosts
    panel: PanelShip
    rubric: tuple[RubricDay, ...]
    agents: tuple[AgentCost, ...]

    @property
    def empty(self) -> bool:
        """Whether the window holds neither a trace row nor a work-item event."""
        return self.trace_rows == 0 and self.events == 0

    def to_json(self) -> dict[str, Any]:
        """Return the report as a JSON-ready dict; `team` is not a key, see the module docstring."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "empty": self.empty,
            "trace_rows": self.trace_rows,
            "events": self.events,
            "tickets": self.tickets,
            "time_in_state": [entry.to_json() for entry in self.time_in_state],
            "escalations": self.escalations.to_json(),
            "costs": self.costs.to_json(),
            "panel": self.panel.to_json(),
            "rubric": [day.to_json() for day in self.rubric],
            "agents": [agent.to_json() for agent in self.agents],
        }


def factory_report(
    con: duckdb.DuckDBPyConnection,
    start: datetime.datetime,
    end: datetime.datetime,
    scoring: ScoringSpec,
    *,
    now: datetime.datetime | None = None,
) -> FactoryReport:
    """Run every query for the window `[start, end)` and return the report.

    `scoring` is the judge manifest's `scoring` block, which turns a judge's four scores into the
    ship decision. `now` is the clock, for tests: a ticket still in a state is counted in it up to
    `min(end, now)`. Reads only. Raises `ValueError` when `start` is not before `end` or either is
    naive.
    """
    _check_window(start, end)
    now = now or datetime.datetime.now(datetime.UTC)
    history = _history(con)
    return FactoryReport(
        start=start,
        end=end,
        trace_rows=_count(con, "traces", "started_at", start, end),
        events=_count(con, "work_item_events", '"at"', start, end),
        tickets=tickets_by_state(con, end, history=history),
        time_in_state=time_in_state(con, start, end, now=now, history=history),
        escalations=escalations(con, start, end),
        costs=ticket_costs(con, start, end, history=history),
        panel=panel_ship(con, start, end, scoring),
        rubric=rubric_scores(con, start, end),
        agents=agent_costs(con, start, end),
    )


def tickets_by_state(
    con: duckdb.DuckDBPyConnection,
    end: datetime.datetime,
    *,
    history: History | None = None,
) -> dict[str, int]:
    """Return how many tickets were in each state just before `end`, every state listed.

    A ticket with no event before `end` did not exist yet and is not counted. `history` is the
    result of an earlier replay of the events, to save a second read.
    """
    history = _history(con) if history is None else history
    counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
    for events in history.values():
        state = _state_at(events, end - datetime.timedelta(microseconds=1))
        if state is not None:
            counts[state] += 1
    return counts


def time_in_state(
    con: duckdb.DuckDBPyConnection,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    now: datetime.datetime,
    history: History | None = None,
) -> tuple[StateTime, ...]:
    """Return the seconds the tickets spent in each state within `[start, min(end, now))`.

    Every state but `done` and `dropped` is listed, in lifecycle order, with the number of tickets
    that spent any time there. A ticket's time in a state runs from the event that moved it in to
    the event that moved it out, or to `min(end, now)` while it is still there, clipped to the
    window.
    """
    history = _history(con) if history is None else history
    cap = min(end, now)
    seconds: dict[str, float] = defaultdict(float)
    tickets: dict[str, set[str]] = defaultdict(set)
    for item_id, events in history.items():
        for index, (entered, state) in enumerate(events):
            left = events[index + 1][0] if index + 1 < len(events) else cap
            lo, hi = max(entered, start), min(left, cap)
            if state in END_STATES or hi <= lo:
                continue
            seconds[state] += (hi - lo).total_seconds()
            tickets[state].add(item_id)
    return tuple(
        StateTime(state, seconds[state], len(tickets[state]))
        for state in STATUSES
        if state not in END_STATES
    )


def escalations(
    con: duckdb.DuckDBPyConnection, start: datetime.datetime, end: datetime.datetime
) -> Escalations:
    """Count the moves into `escalated` and into `blocked` in `[start, end)`, and the tickets active.

    A ticket is active when it has any event in the window. The escalated count is a read of
    `work_item_events` that #93 (escalation records) may refine.
    """
    row = con.execute(
        """
        SELECT count(*) FILTER (WHERE to_status = 'escalated'),
               count(*) FILTER (WHERE to_status = 'blocked'),
               count(DISTINCT work_item_id)
        FROM work_item_events WHERE "at" >= ? AND "at" < ?
        """,
        [start, end],
    ).fetchone()
    escalated, blocked, tickets = row if row else (0, 0, 0)
    return Escalations(int(escalated), int(blocked), int(tickets))


def ticket_costs(
    con: duckdb.DuckDBPyConnection,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    history: History | None = None,
) -> TicketCosts:
    """Return the cost of the tickets' runs started in `[start, end)`, per ticket and per phase.

    The phase of a trace row is the state its ticket was in when the row started, `UNATTRIBUTED`
    when the row is older than the ticket's first event.
    """
    history = _history(con) if history is None else history
    owners: dict[str, list[str]] = defaultdict(list)
    for item_id, run_ids in con.execute(_RUNS).fetchall():
        for run_id in json.loads(run_ids):
            owners[run_id].append(item_id)
    per_ticket: dict[str, float] = {}
    per_phase: dict[str, float] = {}
    total = 0.0
    if owners:
        for run_id, started, cost in con.execute(_ROWS, [list(owners), start, end]).fetchall():
            started = started.replace(tzinfo=datetime.UTC)
            for item_id in owners[run_id]:
                phase = _state_at(history.get(item_id, []), started) or UNATTRIBUTED
                per_ticket[item_id] = per_ticket.get(item_id, 0.0) + cost
                per_phase[phase] = per_phase.get(phase, 0.0) + cost
                total += cost
    return TicketCosts(total, dict(sorted(per_ticket.items())), dict(sorted(per_phase.items())))


def panel_ship(
    con: duckdb.DuckDBPyConnection,
    start: datetime.datetime,
    end: datetime.datetime,
    scoring: ScoringSpec,
) -> PanelShip:
    """Return how the review panel's runs started in `[start, end)` came out, from the trace.

    A run's verdict is its judge's weighted score at `scoring.ship_threshold`; for a run whose
    judge-gate skipped the judge it is the gate's verdict. Where a run has several judge rows the
    last counts. A judge output that does not carry the four scores is counted as `unreadable`.
    Rows tagged `outside:`, the trace rows of outside writes such as a review posted for the judge
    (`runtime/outside.py`), carry the judge's `agent_ref` but are not judge runs and are ignored.
    """
    judged: dict[str, tuple[bool, float] | None] = {}
    for run_id, output in con.execute(
        """
        SELECT pipeline_run_id, output_json FROM traces
        WHERE starts_with(agent_ref, ?) AND kind = 'llm' AND error IS NULL
          AND output_json IS NOT NULL AND pipeline_run_id IS NOT NULL
          AND (tag IS NULL OR NOT starts_with(tag, ?))
          AND started_at >= ? AND started_at < ?
        ORDER BY started_at, rowid
        """,
        [f"{JUDGE_AGENT}@", OUTSIDE_TAG_PREFIX, start, end],
    ).fetchall():
        judged[run_id] = _verdict(json.loads(output), scoring)
    skipped: dict[str, bool] = {}
    for run_id, decision in con.execute(
        """
        SELECT pipeline_run_id, decision_json FROM traces
        WHERE tag = ? AND pipeline_run_id IS NOT NULL AND started_at >= ? AND started_at < ?
        ORDER BY started_at, rowid
        """,
        [JUDGE_GATE_TAG, start, end],
    ).fetchall():
        data = json.loads(decision) if decision else {}
        if data.get("path") == PATH_SKIPPED and run_id not in judged:
            skipped[run_id] = _gate_ships(con, run_id)
    scores = [verdict[1] for verdict in judged.values() if verdict is not None]
    shipped = sum(verdict[0] for verdict in judged.values() if verdict is not None)
    return PanelShip(
        runs=len(scores) + len(skipped),
        judged=len(scores),
        skipped=len(skipped),
        shipped=shipped + sum(skipped.values()),
        unreadable=sum(verdict is None for verdict in judged.values()),
        mean_score=sum(scores) / len(scores) if scores else None,
    )


def rubric_scores(
    con: duckdb.DuckDBPyConnection, start: datetime.datetime, end: datetime.datetime
) -> tuple[RubricDay, ...]:
    """Return the rubric grader's scores in `[start, end)` per UTC day, oldest day first."""
    rows = con.execute(
        """
        SELECT CAST(timezone('UTC', started_at) AS DATE),
               count(*), avg(CAST(json_extract(output_json, '$.score') AS DOUBLE)),
               count(*) FILTER (WHERE CAST(json_extract(output_json, '$.passed') AS BOOLEAN))
        FROM traces
        WHERE starts_with(agent_ref, ?) AND error IS NULL AND output_json IS NOT NULL
          AND json_extract(output_json, '$.score') IS NOT NULL
          AND started_at >= ? AND started_at < ?
        GROUP BY 1 ORDER BY 1
        """,
        [f"{RUBRIC_AGENT}@", start, end],
    ).fetchall()
    return tuple(RubricDay(day, int(n), float(mean), int(passed)) for day, n, mean, passed in rows)


def agent_costs(
    con: duckdb.DuckDBPyConnection, start: datetime.datetime, end: datetime.datetime
) -> tuple[AgentCost, ...]:
    """Return runs, errors and cost per agent version in `[start, end)`, dearest first.

    An `agent_ref` is `name@version`; a ref without an `@` has the version `""`. Rows tagged
    `outside:` are left out: an outside write is recorded under the agent it is made for but is a
    call of the guard, not a run of that agent, costs nothing and would only inflate its `runs`.
    """
    rows = con.execute(
        """
        SELECT agent_ref, count(*), count(*) FILTER (WHERE error IS NOT NULL),
               COALESCE(sum(cost_usd), 0.0)
        FROM traces
        WHERE (tag IS NULL OR NOT starts_with(tag, ?)) AND started_at >= ? AND started_at < ?
        GROUP BY agent_ref ORDER BY 4 DESC, agent_ref
        """,
        [OUTSIDE_TAG_PREFIX, start, end],
    ).fetchall()
    return tuple(
        AgentCost(ref.partition("@")[0], ref.partition("@")[2], int(n), int(errors), float(cost))
        for ref, n, errors, cost in rows
    )


def _check_window(start: datetime.datetime, end: datetime.datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("the window must be timezone-aware")
    if start >= end:
        raise ValueError(f"the window start {start} is not before its end {end}")


def _count(
    con: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> int:
    row = con.execute(
        f"SELECT count(*) FROM {table} WHERE {column} >= ? AND {column} < ?",
        [start, end],
    ).fetchone()
    return int(row[0]) if row else 0


def _history(con: duckdb.DuckDBPyConnection) -> History:
    """Every ticket's `(time, state entered)` list in order, from `work_item_events`."""
    history: dict[str, list[tuple[datetime.datetime, str]]] = defaultdict(list)
    for item_id, at, _, to_status in con.execute(_EVENTS).fetchall():
        history[item_id].append((at.replace(tzinfo=datetime.UTC), to_status))
    return dict(history)


def _state_at(
    events: Iterable[tuple[datetime.datetime, str]], when: datetime.datetime
) -> str | None:
    """The state entered by the last event at or before `when`, or `None` before the first."""
    state = None
    for at, to_status in events:
        if at > when:
            break
        state = to_status
    return state


def _verdict(output: Any, scoring: ScoringSpec) -> tuple[bool, float] | None:
    """Return `(ships, weighted score)` for a judge output, or `None` when it has no four scores."""
    try:
        score = weighted_score(
            score_values(output if isinstance(output, dict) else {}), scoring.weights
        )
    except KeyError, TypeError, ValueError:
        return None
    return ship(score, scoring.ship_threshold), score


def _gate_ships(con: duckdb.DuckDBPyConnection, run_id: str) -> bool:
    """Whether the judge-gate row of `run_id` answered `ship` (the last one wins)."""
    row = con.execute(
        """
        SELECT output_json FROM traces WHERE tag = ? AND pipeline_run_id = ?
        ORDER BY started_at DESC, rowid DESC LIMIT 1
        """,
        [JUDGE_GATE_TAG, run_id],
    ).fetchone()
    answers = json.loads(row[0]) if row and row[0] else {}
    verdict = answers.get("verdict") if isinstance(answers, dict) else None
    return isinstance(verdict, dict) and verdict.get("answer") == "ship"


__all__ = [
    "END_STATES",
    "AgentCost",
    "Escalations",
    "FactoryReport",
    "PanelShip",
    "RubricDay",
    "StateTime",
    "TicketCosts",
    "agent_costs",
    "escalations",
    "factory_report",
    "open_store",
    "panel_ship",
    "rubric_scores",
    "ticket_costs",
    "tickets_by_state",
    "time_in_state",
]
