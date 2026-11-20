"""Tests for runtime/reporting.py, over a store seeded with `WorkItems` and `Trace.record`.

The seed is three tickets over 16 to 20 November 2026 and the trace rows of their runs; the window
of most tests is those five days, with the clock at its end.
"""

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pytest

from factory.exit import ExitCode, exit_code
from pipelines.review_panel import JUDGE_GATE_TAG
from registry import load_spec
from registry.common_schemas import Ticket
from registry.spec import LLMSpec, ScoringSpec
from runtime import reporting
from runtime.reporting import UNATTRIBUTED, open_store
from runtime.trace import Trace, TraceRow
from runtime.work_items import STATUSES, WorkItems

ROOT = Path(__file__).parents[2]
START = datetime.datetime(2026, 11, 16, tzinfo=datetime.UTC)
END = datetime.datetime(2026, 11, 21, tzinfo=datetime.UTC)
TICKET = Ticket(
    kind="feature",
    title="Add the thing",
    body="Context.",
    acceptance=["the thing exists"],
    repo="agb1986/agb-agent-factory",
    ref="main",
)
SHIP = {"correctness_risk": 0.1, "security_risk": 0.1, "test_coverage": 0.9, "maintainability": 0.9}
"""Judge scores whose weighted score is 0.9."""
REJECT = {
    "correctness_risk": 0.8,
    "security_risk": 0.8,
    "test_coverage": 0.3,
    "maintainability": 0.3,
}
"""Judge scores whose weighted score is 0.235."""


def at(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    """A UTC time in November 2026."""
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=datetime.UTC)


def scoring() -> ScoringSpec:
    """The scoring block of the real judge manifest."""
    spec = load_spec(ROOT / "agents" / "judge" / "agent.yaml")
    assert isinstance(spec, LLMSpec) and spec.scoring is not None
    return spec.scoring


class Clock:
    """A clock the test sets before each store call."""

    def __init__(self) -> None:
        self.now = at(16, 9)

    def __call__(self) -> datetime.datetime:
        return self.now


class Seed:
    """A store in `tmp_path` with the tickets and trace rows described in the module docstring."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "traces.duckdb"
        self.clock = Clock()

    def row(
        self,
        run: str | None,
        when: datetime.datetime,
        agent_ref: str,
        cost: float | None = None,
        *,
        kind: str = "llm",
        tag: str | None = None,
        decision: Any = None,
        output: Any = None,
        error: str | None = None,
    ) -> None:
        """Write one trace row."""
        with Trace(self.path) as trace:
            trace.record(
                TraceRow(
                    run_id=str(uuid.uuid4()),
                    agent_ref=agent_ref,
                    kind=kind,
                    started_at=when,
                    duration_ms=1,
                    input_json={"prompt": "UNTRUSTED"},
                    pipeline_run_id=run,
                    cost_usd=cost,
                    decision_json=decision,
                    output_json=output,
                    error=error,
                    tag=tag,
                )
            )

    def tickets(self) -> None:
        """Write wi-1 (done), wi-2 (escalated) and wi-3 (still todo), with their events and runs."""
        with Trace(self.path) as trace:
            items = WorkItems(trace, now=self.clock)

            def step(when: datetime.datetime, item: str, to: str) -> None:
                self.clock.now = when
                items.move(item, to, "test")

            self.clock.now = at(16, 9)
            items.add(TICKET)
            step(at(16, 10), "wi-1", "ready")
            step(at(16, 11), "wi-1", "in_progress")
            step(at(16, 12), "wi-1", "in_review")
            step(at(16, 13), "wi-1", "done")
            self.clock.now = at(17, 9)
            items.add(TICKET)
            step(at(17, 10), "wi-2", "ready")
            step(at(17, 11), "wi-2", "in_progress")
            step(at(17, 12), "wi-2", "blocked")
            step(at(17, 13), "wi-2", "escalated")
            self.clock.now = at(19, 9)
            items.add(TICKET)
            items.link_run("wi-1", "run-a")
            items.link_run("wi-2", "run-b")

    def runs(self) -> None:
        """Write the trace rows: the tickets' runs, a panel's rows, rubric rows and noise."""
        coder = "feature-coder@0.1.0"
        self.row("run-a", at(16, 11, 30), coder, 0.20)
        self.row("run-a", at(16, 12, 30), "judge@2.0.0", 0.10, output={"scores": _scores(SHIP)})
        self.row("run-b", at(17, 8), coder, 0.01)
        self.row("run-b", at(17, 11, 30), coder, 0.30)
        self.row("run-b", at(17, 12, 30), coder, 0.05, error="boom")
        self.row("run-b", at(17, 13, 30), "judge@2.0.0", 0.10, output={"scores": _scores(REJECT)})
        self.row("run-x", at(18, 10), "code-reviewer@0.1.0", 0.50)
        self.row("run-z", at(18, 10), "judge@2.0.0", 0.10, output={"scores": {}})
        for run, answer in (("run-s", "ship"), ("run-u", "request_changes")):
            self.row(
                run,
                at(18, 11),
                "judge-gate@1.0.0",
                0.0,
                kind="jev",
                tag=JUDGE_GATE_TAG,
                decision={"path": "skipped_judge"},
                output={"verdict": {"answer": answer}},
            )
        self.row(
            "run-j",
            at(18, 11),
            "judge-gate@1.0.0",
            0.0,
            kind="jev",
            tag=JUDGE_GATE_TAG,
            decision={"path": "judge"},
            output={"verdict": {"answer": "ship"}},
        )
        rubric = "eval-rubric@some-model"
        self.row("eval", at(18, 10), rubric, 0.02, output={"passed": True, "score": 0.8})
        self.row("eval", at(18, 15), rubric, 0.02, output={"passed": False, "score": 0.4})
        self.row("eval", at(19, 10), rubric, 0.02, output={"passed": True, "score": 1.0})
        self.row("run-a", at(10, 11), coder, 9.99)  # before the window


def _scores(values: dict[str, float]) -> dict[str, dict[str, float]]:
    return {name: {"value": value} for name, value in values.items()}


@pytest.fixture
def seed(tmp_path: Path) -> Seed:
    store = Seed(tmp_path)
    store.tickets()
    store.runs()
    return store


@pytest.fixture
def con(seed: Seed) -> Any:
    with open_store(seed.path) as connection:
        yield connection


def test_tickets_are_counted_in_the_state_they_were_in_at_the_end(con: Any) -> None:
    counts = reporting.tickets_by_state(con, END)

    assert list(counts) == list(STATUSES)
    assert counts == {**dict.fromkeys(STATUSES, 0), "todo": 1, "escalated": 1, "done": 1}


def test_tickets_are_counted_as_they_were_at_an_earlier_end(con: Any) -> None:
    counts = reporting.tickets_by_state(con, at(17, 12, 30))

    assert counts["done"] == 1
    assert counts["blocked"] == 1
    assert counts["todo"] == 0  # wi-3 did not exist yet


def test_time_in_state_sums_the_tickets_and_excludes_the_end_states(con: Any) -> None:
    entries = {e.state: e for e in reporting.time_in_state(con, START, END, now=END)}

    hours = {state: entry.seconds / 3600 for state, entry in entries.items()}
    assert hours == {
        "todo": 41,  # 1h + 1h + wi-3's 39h
        "ready": 2,
        "in_progress": 2,
        "in_review": 1,
        "blocked": 1,
        "escalated": 83,  # wi-2, from 17 Nov 13:00 to the end
    }
    assert entries["todo"].tickets == 3
    assert entries["escalated"].tickets == 1


def test_time_in_state_is_clipped_to_the_window_and_to_now(con: Any) -> None:
    clipped = {
        e.state: e.seconds / 3600
        for e in reporting.time_in_state(con, at(17, 10, 30), at(18, 0), now=END)
    }
    assert clipped["ready"] == 0.5  # wi-2 was ready from 10:00; the window opens at 10:30
    assert clipped["in_progress"] == 1
    assert clipped["escalated"] == 11  # 17 Nov 13:00 to 18 Nov 00:00

    early = {
        e.state: e.seconds / 3600 for e in reporting.time_in_state(con, START, END, now=at(19, 11))
    }
    assert early["todo"] == 1 + 1 + 2  # wi-3 stops counting at `now`


def test_escalations_count_moves_into_escalated_and_blocked(con: Any) -> None:
    result = reporting.escalations(con, START, END)

    assert (result.escalated, result.blocked, result.tickets) == (1, 1, 3)
    assert result.per_ticket == pytest.approx(1 / 3)


def test_escalations_are_windowed(con: Any) -> None:
    result = reporting.escalations(con, at(18, 0), END)

    assert (result.escalated, result.blocked, result.tickets) == (0, 0, 1)
    assert result.per_ticket == 0


def test_escalations_per_ticket_is_none_without_a_ticket() -> None:
    assert reporting.Escalations(0, 0, 0).per_ticket is None


def test_cost_is_per_ticket_and_per_phase_and_skips_other_runs(con: Any) -> None:
    costs = reporting.ticket_costs(con, START, END)

    assert costs.per_ticket == pytest.approx({"wi-1": 0.30, "wi-2": 0.46})
    assert costs.per_phase == pytest.approx(
        {
            "in_progress": 0.50,
            "in_review": 0.10,
            "blocked": 0.05,
            "escalated": 0.10,
            UNATTRIBUTED: 0.01,
        }
    )
    assert costs.total_usd == pytest.approx(0.76)
    assert costs.mean_per_ticket_usd == pytest.approx(0.38)


def test_cost_leaves_out_rows_outside_the_window(con: Any) -> None:
    costs = reporting.ticket_costs(con, at(17, 0), END)

    assert costs.per_ticket == pytest.approx({"wi-2": 0.46})


def test_a_run_linked_to_two_tickets_counts_for_both(seed: Seed) -> None:
    with Trace(seed.path) as trace:
        seed.clock.now = at(19, 12)
        WorkItems(trace, now=seed.clock).link_run("wi-3", "run-a")

    with open_store(seed.path) as con:
        costs = reporting.ticket_costs(con, START, END)

    assert costs.per_ticket["wi-3"] == pytest.approx(0.30)
    assert costs.per_phase[UNATTRIBUTED] == pytest.approx(0.01 + 0.30)


def test_cost_without_tickets_is_zero_and_the_mean_is_none(tmp_path: Path) -> None:
    store = Seed(tmp_path)
    store.row("run-a", at(17, 9), "judge@2.0.0", 1.0)

    with open_store(store.path) as con:
        costs = reporting.ticket_costs(con, START, END)

    assert costs.total_usd == 0
    assert costs.mean_per_ticket_usd is None
    assert costs.per_ticket == {} and costs.per_phase == {}


def test_panel_ship_rate_counts_judged_and_skipped_runs(con: Any) -> None:
    panel = reporting.panel_ship(con, START, END, scoring())

    assert (panel.runs, panel.judged, panel.skipped) == (4, 2, 2)
    assert (panel.shipped, panel.unreadable) == (2, 1)
    assert panel.ship_rate == 0.5
    assert panel.mean_score == pytest.approx((0.9 + 0.235) / 2)


def test_an_outside_write_row_for_the_judge_is_not_a_judge_run(tmp_path: Path) -> None:
    store = Seed(tmp_path)
    store.row("run-a", at(17, 9), "judge@2.1.0", 0.1, output={"scores": _scores(SHIP)})
    store.row(
        "run-a",
        at(17, 10),
        "judge@2.1.0",
        0.0,
        tag="outside:github.comment_pr",
        output={"action": "comment_pr", "url": "https://example.test/1", "body": "ok"},
    )

    with open_store(store.path) as con:
        panel = reporting.panel_ship(con, START, END, scoring())
        (agent,) = reporting.agent_costs(con, START, END)

    assert (panel.judged, panel.shipped, panel.unreadable) == (1, 1, 0)
    assert panel.ship_rate == 1.0
    assert (agent.runs, agent.cost_usd) == (1, pytest.approx(0.1))  # the write is not a run


def test_panel_ship_follows_the_threshold(con: Any) -> None:
    strict = scoring().model_copy(update={"ship_threshold": 0.95})

    panel = reporting.panel_ship(con, START, END, strict)

    assert panel.shipped == 1  # only the gate's `ship`; the 0.9 judge run no longer ships


def test_panel_ship_rate_is_none_without_a_verdict(tmp_path: Path) -> None:
    store = Seed(tmp_path)
    store.row("run-a", at(17, 9), "feature-coder@0.1.0", 1.0)

    with open_store(store.path) as con:
        panel = reporting.panel_ship(con, START, END, scoring())

    assert panel.ship_rate is None and panel.mean_score is None


def test_rubric_scores_are_averaged_per_utc_day(con: Any) -> None:
    days = reporting.rubric_scores(con, START, END)

    assert [(d.day, d.scores, d.passed) for d in days] == [
        (datetime.date(2026, 11, 18), 2, 1),
        (datetime.date(2026, 11, 19), 1, 1),
    ]
    assert [d.mean_score for d in days] == pytest.approx([0.6, 1.0])


def test_agent_costs_group_by_version_dearest_first(con: Any) -> None:
    agents = reporting.agent_costs(con, START, END)

    by_ref = {f"{a.agent}@{a.version}": a for a in agents}
    assert agents[0].agent == "feature-coder"  # 0.56 beats code-reviewer's 0.50
    assert by_ref["feature-coder@0.1.0"].runs == 4
    assert by_ref["feature-coder@0.1.0"].errors == 1
    assert by_ref["feature-coder@0.1.0"].cost_usd == pytest.approx(0.56)
    assert by_ref["judge@2.0.0"].runs == 3
    assert [a.cost_usd for a in agents] == sorted((a.cost_usd for a in agents), reverse=True)


def test_agent_costs_count_an_unpriced_row_as_zero(tmp_path: Path) -> None:
    store = Seed(tmp_path)
    store.row("run-a", at(17, 9), "judge@2.0.0", None)

    with open_store(store.path) as con:
        (entry,) = reporting.agent_costs(con, START, END)

    assert (entry.runs, entry.cost_usd) == (1, 0.0)


def test_the_report_holds_every_measure_and_is_json(con: Any) -> None:
    report = reporting.factory_report(con, START, END, scoring(), now=END)

    assert not report.empty
    assert report.events == 11
    assert report.trace_rows == 14  # the seed's 15 rows less the one before the window
    document = report.to_json()
    assert json.loads(json.dumps(document)) == document
    assert document["start"] == "2026-11-16T00:00:00+00:00"
    assert document["empty"] is False
    assert document["costs"]["total_usd"] == pytest.approx(0.76)
    assert document["panel"]["ship_rate"] == 0.5
    assert document["escalations"]["escalated"] == 1
    assert "team" not in json.dumps(document)


def test_a_window_without_rows_or_events_is_empty(con: Any) -> None:
    report = reporting.factory_report(con, at(1, 0), at(2, 0), scoring(), now=END)

    assert report.empty
    assert report.to_json()["empty"] is True
    assert report.tickets == dict.fromkeys(STATUSES, 0)


def test_a_store_without_work_items_still_reports_its_trace(tmp_path: Path) -> None:
    store = Seed(tmp_path)
    store.row("run-a", at(17, 9), "judge@2.0.0", 1.0, output={"scores": _scores(SHIP)})

    with open_store(store.path) as con:
        report = reporting.factory_report(con, START, END, scoring(), now=END)

    assert not report.empty and report.events == 0
    assert report.costs.total_usd == 0
    assert report.panel.shipped == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (END, START),
        (START, START),
        (datetime.datetime(2026, 11, 16), END),
    ],
)
def test_a_bad_window_is_a_value_error(con: Any, start: datetime.datetime, end: Any) -> None:
    with pytest.raises(ValueError, match="window"):
        reporting.factory_report(con, start, end, scoring())


def test_the_queries_only_read(seed: Seed) -> None:
    before = seed.path.read_bytes()

    with open_store(seed.path) as con:
        reporting.factory_report(con, START, END, scoring(), now=END)
        with pytest.raises(duckdb.InvalidInputException):
            con.execute("DELETE FROM traces")

    assert seed.path.read_bytes() == before


def test_open_store_names_a_missing_store(tmp_path: Path) -> None:
    with (
        pytest.raises(reporting.NotFoundError, match="no trace store"),
        open_store(tmp_path / "missing.duckdb"),
    ):
        pass


def test_open_store_names_a_store_without_the_tables(tmp_path: Path) -> None:
    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).close()

    with pytest.raises(reporting.NotFoundError) as caught, open_store(path) as con:
        reporting.factory_report(con, START, END, scoring())

    assert exit_code(caught.value) == ExitCode.NOT_FOUND


def test_the_judge_gate_tag_is_the_review_panels() -> None:
    assert reporting.JUDGE_GATE_TAG == JUDGE_GATE_TAG
