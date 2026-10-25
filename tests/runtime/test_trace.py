"""Tests for runtime/trace.py."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from runtime.trace import (
    DEFAULT_PATH,
    ENV_VAR,
    MIGRATIONS,
    CostRow,
    Trace,
    TraceRow,
    resolve_path,
)

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def row(run_id: str, **kw: Any) -> TraceRow:
    base = TraceRow(
        run_id=run_id,
        agent_ref="code-reviewer@1.2.0",
        kind="llm",
        started_at=T0,
        duration_ms=1200,
        input_json={"diff": "x"},
    )
    return replace(base, **kw)


@pytest.fixture
def trace(tmp_path: Path):
    with Trace(tmp_path / "t.duckdb") as t:
        yield t


def test_ensure_schema_twice_is_a_noop(trace: Trace) -> None:
    trace.ensure_schema()
    trace.record(row("r1"))
    trace.ensure_schema()
    assert trace.schema_version() == len(MIGRATIONS)
    assert trace._connect().execute("SELECT count(*) FROM schema_version").fetchone() == (
        len(MIGRATIONS),
    )
    assert trace._connect().execute("SELECT count(*) FROM traces").fetchone() == (1,)


def test_record_round_trips_every_column(trace: Trace) -> None:
    trace.record(
        row(
            "r1",
            pipeline_run_id="p1",
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=30,
            cache_write_tokens=40,
            cost_usd=0.5,
            decision_json={"skip": False},
            output_json={"ok": True},
            error="E: boom",
            tag="eval:code-reviewer@1.2.0",
        )
    )
    got = (
        trace._connect()
        .execute(
            "SELECT run_id, pipeline_run_id, agent_ref, kind, duration_ms, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, "
            "decision_json::VARCHAR, input_json::VARCHAR, output_json::VARCHAR, error, tag, "
            "epoch(started_at) FROM traces"
        )
        .fetchone()
    )
    assert got == (
        "r1", "p1", "code-reviewer@1.2.0", "llm", 1200, 10, 20, 30, 40, 0.5,
        '{"skip": false}', '{"diff": "x"}', '{"ok": true}', "E: boom",
        "eval:code-reviewer@1.2.0", T0.timestamp(),
    )  # fmt: skip


def test_nullable_columns_stay_null(trace: Trace) -> None:
    trace.record(row("r1"))
    got = (
        trace._connect()
        .execute(
            "SELECT pipeline_run_id, input_tokens, cost_usd, decision_json, output_json, error, tag "
            "FROM traces"
        )
        .fetchone()
    )
    assert got == (None,) * 7


def test_naive_started_at_is_rejected(trace: Trace) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        trace.record(row("r1", started_at=datetime(2026, 10, 25)))


def test_kind_is_checked(trace: Trace) -> None:
    import duckdb

    with pytest.raises(duckdb.ConstraintException):
        trace.record(row("r1", kind="other"))


def test_cost_by_agent(trace: Trace) -> None:
    trace.record(row("r1", cost_usd=0.25))
    trace.record(row("r2", cost_usd=0.5))
    trace.record(row("r3", agent_ref="security-reviewer@0.1.0", cost_usd=1.0))
    trace.record(row("r4", agent_ref="jev-gate@1.0.0", kind="jev"))
    assert trace.cost_by_agent() == {
        "code-reviewer@1.2.0": 0.75,
        "jev-gate@1.0.0": 0.0,
        "security-reviewer@0.1.0": 1.0,
    }


def test_cost_by_agent_since(trace: Trace) -> None:
    trace.record(row("old", started_at=T0 - timedelta(days=2), cost_usd=5.0))
    trace.record(row("new", cost_usd=1.0))
    assert trace.cost_by_agent(since=T0 - timedelta(days=1)) == {"code-reviewer@1.2.0": 1.0}
    assert trace.cost_by_agent(since=T0 + timedelta(days=1)) == {}


def test_cost_by_pipeline_run(trace: Trace) -> None:
    trace.record(row("r1", pipeline_run_id="p1", cost_usd=0.25))
    trace.record(row("r2", pipeline_run_id="p1", cost_usd=0.5))
    trace.record(row("r3", pipeline_run_id="p2", cost_usd=9.0))
    trace.record(row("r4", pipeline_run_id="p1"))
    assert trace.cost_by_pipeline_run("p1") == 0.75
    assert trace.cost_by_pipeline_run("missing") == 0.0


def test_second_migration_bumps_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "t.duckdb"
    with Trace(path) as t:
        t.ensure_schema()
        assert t.schema_version() == len(MIGRATIONS)
    extra = (len(MIGRATIONS) + 1, "ALTER TABLE traces ADD COLUMN model VARCHAR")
    with Trace(path, migrations=(*MIGRATIONS, extra)) as t:
        t.ensure_schema()
        t.ensure_schema()
        assert t.schema_version() == len(MIGRATIONS) + 1
        assert t._connect().execute("SELECT count(*) FROM schema_version").fetchone() == (
            len(MIGRATIONS) + 1,
        )
        t._connect().execute("SELECT model FROM traces")


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    import duckdb

    bad = (len(MIGRATIONS) + 1, "ALTER TABLE no_such_table ADD COLUMN x VARCHAR")
    with Trace(tmp_path / "t.duckdb", migrations=(*MIGRATIONS, bad)) as t:
        with pytest.raises(duckdb.Error):
            t.ensure_schema()
        assert t.schema_version() == len(MIGRATIONS)


def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "t.duckdb"
    with Trace(path) as t:
        t.record(row("r1", cost_usd=1.0))
    with Trace(path) as t:
        assert t.cost_by_agent() == {"code-reviewer@1.2.0": 1.0}


def test_path_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_path() == DEFAULT_PATH
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "env.duckdb"))
    assert resolve_path() == tmp_path / "env.duckdb"
    assert resolve_path(tmp_path / "arg.duckdb") == tmp_path / "arg.duckdb"


def test_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "data" / "nested" / "t.duckdb"
    with Trace(path) as t:
        t.ensure_schema()
    assert path.exists()


def test_record_published_appends_one_row(trace: Trace) -> None:
    trace.record_published("code-reviewer", "1.0.0", 0.75, "abc123")
    [found] = trace.published()
    assert (found.agent, found.version, found.pass_rate, found.git_sha) == (
        "code-reviewer",
        "1.0.0",
        0.75,
        "abc123",
    )
    assert found.published_at.tzinfo is not None


def test_published_filters_by_agent_and_keeps_history(trace: Trace) -> None:
    trace.record_published("a", "1.0.0", 1.0, "s1")
    trace.record_published("a", "1.0.0", 0.9, "s2")
    trace.record_published("b", "2.0.0", 1.0, "s3")
    assert [r.git_sha for r in trace.published("a")] == ["s1", "s2"]
    assert len(trace.published()) == 3


def test_record_published_rejects_naive_timestamp(trace: Trace) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        trace.record_published("a", "1.0.0", 1.0, "s", datetime(2026, 1, 1))


# --- cost_breakdown ------------------------------------------------------------------------------


def test_cost_breakdown_groups_by_agent_and_tag_bucket(trace: Trace) -> None:
    trace.record(row("r1", cost_usd=0.25))
    trace.record(row("r2", cost_usd=0.5, tag="eval:code-reviewer@1.2.0"))
    trace.record(row("r3", cost_usd=1.0, tag="eval:code-reviewer@1.2.0"))
    trace.record(row("r4", cost_usd=2.0, tag="publish:code-reviewer@1.2.0"))
    trace.record(row("r5", cost_usd=4.0, tag="nightly"))
    trace.record(row("r6", agent_ref="fixer@0.1.0", cost_usd=8.0))
    assert trace.cost_breakdown(T0) == [
        CostRow("code-reviewer@1.2.0", "eval", 2, 1.5),
        CostRow("code-reviewer@1.2.0", "other", 2, 4.25),
        CostRow("code-reviewer@1.2.0", "publish", 1, 2.0),
        CostRow("fixer@0.1.0", "other", 1, 8.0),
    ]


def test_cost_breakdown_groups_by_pipeline_with_runs_outside_one_last(trace: Trace) -> None:
    trace.record(row("r1", pipeline_run_id="p2", cost_usd=1.0))
    trace.record(row("r2", pipeline_run_id="p1", cost_usd=2.0))
    trace.record(row("r3", pipeline_run_id="p1", cost_usd=3.0))
    trace.record(row("r4", cost_usd=4.0))
    assert trace.cost_breakdown(T0, by="pipeline") == [
        CostRow("p1", "other", 2, 5.0),
        CostRow("p2", "other", 1, 1.0),
        CostRow(None, "other", 1, 4.0),
    ]


def test_cost_breakdown_counts_a_run_without_a_cost_as_free(trace: Trace) -> None:
    trace.record(row("r1"))
    assert trace.cost_breakdown(T0) == [CostRow("code-reviewer@1.2.0", "other", 1, 0.0)]


def test_cost_breakdown_starts_at_since_inclusive(trace: Trace) -> None:
    trace.record(row("old", started_at=T0 - timedelta(seconds=1), cost_usd=1.0))
    trace.record(row("edge", started_at=T0, cost_usd=2.0))
    trace.record(row("new", started_at=T0 + timedelta(hours=1), cost_usd=4.0))
    [only] = trace.cost_breakdown(T0)
    assert (only.runs, only.cost_usd) == (2, 6.0)


def test_cost_breakdown_reads_since_in_any_offset(trace: Trace) -> None:
    trace.record(row("r1", cost_usd=1.0))
    plus_two = timezone(timedelta(hours=2))
    assert trace.cost_breakdown(T0.astimezone(plus_two))[0].runs == 1
    assert trace.cost_breakdown(T0.astimezone(plus_two) + timedelta(seconds=1)) == []


def test_cost_breakdown_on_an_empty_trace_is_empty(trace: Trace) -> None:
    assert trace.cost_breakdown(T0) == []


def test_cost_breakdown_rejects_a_naive_since(trace: Trace) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        trace.cost_breakdown(datetime(2026, 10, 25))
