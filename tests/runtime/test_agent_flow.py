"""Tests for runtime/agent_flow.py and the `factory trace` subcommand."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory.cli import main
from factory.exit import ExitCode, UsageError
from runtime.agent_flow import events
from runtime.trace import ENV_VAR, Trace, TraceRow

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def row(run_id: str, offset_s: float, duration_ms: int, **kwargs) -> TraceRow:
    return TraceRow(
        run_id=run_id,
        agent_ref="code-reviewer@1.2.0",
        kind="llm",
        started_at=T0 + timedelta(seconds=offset_s),
        duration_ms=duration_ms,
        input_json={},
        pipeline_run_id="p1",
        **kwargs,
    )


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "t.duckdb"
    monkeypatch.setenv(ENV_VAR, str(path))
    with Trace(path) as trace:
        trace.record(row("aaaaaaaa", 0, 2000, input_tokens=10, output_tokens=5, cost_usd=0.01))
        trace.record(row("bbbbbbbb", 1, 3000, decision_json={"route": "fix"}))
    return path


def test_events_are_well_formed_and_ordered(db: Path) -> None:
    with Trace(db) as trace:
        found = list(events("p1", trace.pipeline_runs("p1")))
    assert found[0]["type"] == "agent_spawn" and found[0]["payload"]["isMain"] is True
    assert found[-1]["type"] == "agent_complete" and found[-1]["time"] == 4.0
    assert [e["time"] for e in found] == sorted(e["time"] for e in found)
    assert {e["sessionId"] for e in found} == {"p1"}
    dispatches = [e for e in found if e["type"] == "subagent_dispatch"]
    assert [e["time"] for e in dispatches] == [0.0, 1.0]


def test_cli_writes_jsonl(db: Path, tmp_path: Path) -> None:
    out = tmp_path / "run.jsonl"
    assert main(["trace", "p1", "--out", str(out)]) == ExitCode.OK
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["payload"]["name"] == "pipeline"


def test_unknown_pipeline_run_is_a_usage_error(db: Path) -> None:
    with Trace(db) as trace, pytest.raises(UsageError):
        list(events("nope", trace.pipeline_runs("nope")))


def test_stretch_scales_event_times(db: Path) -> None:
    with Trace(db) as trace:
        base = list(events("p1", trace.pipeline_runs("p1")))
        slow = list(events("p1", trace.pipeline_runs("p1"), stretch=10))
    assert [e["time"] * 10 for e in base] == pytest.approx([e["time"] for e in slow])
    assert slow[-1]["time"] == 40.0


def test_non_positive_stretch_is_a_usage_error(db: Path) -> None:
    with Trace(db) as trace, pytest.raises(UsageError):
        list(events("p1", trace.pipeline_runs("p1"), stretch=0))


def test_parent_run_id_hangs_a_run_under_its_parent(tmp_path: Path) -> None:
    with Trace(tmp_path / "t.duckdb") as trace:
        trace.record(row("aaaaaaaa", 0, 1000))
        trace.record(row("bbbbbbbb", 1, 1000, parent_run_id="aaaaaaaa"))
        trace.record(row("cccccccc", 2, 1000, parent_run_id="not-in-this-run"))
        found = list(events("p1", trace.pipeline_runs("p1")))
    parents = {
        e["payload"]["child"].split("#")[1]: e["payload"]["parent"]
        for e in found
        if e["type"] == "subagent_dispatch"
    }
    assert parents == {
        "aaaaaa": "pipeline",
        "bbbbbb": "code-reviewer@1.2.0 #aaaaaa",
        "cccccc": "pipeline",
    }
