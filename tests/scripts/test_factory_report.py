"""Tests for scripts/factory_report.py, over stores written with `Trace.record` and `WorkItems`."""

import datetime
import importlib.util
import json
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from factory.exit import ExitCode
from registry.common_schemas import Ticket
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItems

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "factory_report.py"
JUDGE_MANIFEST = ROOT / "agents" / "judge" / "agent.yaml"
TODAY = datetime.date(2026, 11, 20)
MARKER = "UNTRUSTED-PROMPT-CONTENT"
"""Every fake prompt contains this, so a test can check none is printed."""
SHIP = {
    name: {"value": value}
    for name, value in {
        "correctness_risk": 0.1,
        "security_risk": 0.1,
        "test_coverage": 0.9,
        "maintainability": 0.9,
    }.items()
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("factory_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def _at(day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 11, day, hour, tzinfo=datetime.UTC)


def _store(tmp_path: Path) -> Path:
    """A store with one ticket moved to `in_progress` on 18 Nov and one judge run linked to it."""
    path = tmp_path / "traces.duckdb"
    clock = {"now": _at(18, 9)}
    ticket = Ticket(
        kind="feature",
        title="Add the thing",
        body="Context.",
        acceptance=["the thing exists"],
        repo="agb1986/agb-agent-factory",
        ref="main",
    )
    with Trace(path) as trace:
        items = WorkItems(trace, now=lambda: clock["now"])
        items.add(ticket)
        clock["now"] = _at(18, 10)
        items.move("wi-1", "ready", "test")
        clock["now"] = _at(18, 11)
        items.move("wi-1", "in_progress", "test")
        items.link_run("wi-1", "run-a")
        trace.record(
            TraceRow(
                run_id=str(uuid.uuid4()),
                agent_ref="judge@2.0.0",
                kind="llm",
                started_at=_at(18, 12),
                duration_ms=1,
                input_json={"prompt": MARKER},
                pipeline_run_id="run-a",
                cost_usd=0.25,
                output_json={"scores": SHIP},
            )
        )
    return path


def _run(
    store: Path, capsys: pytest.CaptureFixture[str], *args: str, manifest: Path = JUDGE_MANIFEST
) -> tuple[int, str, str]:
    code = script.main(
        ["--trace", str(store), "--judge-manifest", str(manifest), *args], today=TODAY
    )
    out, err = capsys.readouterr()
    assert MARKER not in out and MARKER not in err
    return code, out, err


def test_the_markdown_report_names_every_measure(tmp_path: Path, capsys: Any) -> None:
    code, out, _ = _run(_store(tmp_path), capsys, "--since", "2026-11-16", "--until", "2026-11-20")

    assert code == 0
    assert out.startswith("# Factory report, 2026-11-16 to 2026-11-20 (UTC)")
    for section in (
        "## Tickets per state",
        "## Time in each state",
        "## Escalations",
        "## Cost of the tickets' runs",
        "## Review panel",
        "## Rubric scores per day",
        "## Cost per agent version",
    ):
        assert section in out
    assert "- in_progress: 1" in out
    assert "- wi-1: $0.2500" in out
    assert "- judge@2.0.0: $0.2500, 1 runs, 0 errors" in out
    assert "ship rate: 100%" in out
    assert "not grouped by team" in out


def test_the_json_report_is_one_document(tmp_path: Path, capsys: Any) -> None:
    code, out, err = _run(
        _store(tmp_path), capsys, "--since", "2026-11-16", "--until", "2026-11-20", "--json"
    )

    document = json.loads(out)
    assert code == 0 and err == ""
    assert document["empty"] is False
    assert document["start"] == "2026-11-16T00:00:00+00:00"
    assert document["end"] == "2026-11-21T00:00:00+00:00"
    assert document["tickets"]["in_progress"] == 1
    assert document["costs"]["per_ticket"] == {"wi-1": 0.25}
    assert document["panel"]["ship_rate"] == 1.0
    assert "team" not in out


def test_the_window_defaults_to_the_seven_days_ending_today(tmp_path: Path, capsys: Any) -> None:
    code, out, _ = _run(_store(tmp_path), capsys)

    assert code == 0
    assert out.startswith("# Factory report, 2026-11-14 to 2026-11-20 (UTC)")


def test_an_empty_window_says_so_and_exits_one(tmp_path: Path, capsys: Any) -> None:
    code, out, _ = _run(_store(tmp_path), capsys, "--since", "2026-10-01", "--until", "2026-10-07")

    assert code == 1
    assert "No activity in this window" in out
    assert "Nothing to report" in out
    assert "- todo: 0" not in out  # no table of zeros dressed as a result


def test_an_empty_window_in_json_is_marked_and_exits_one(tmp_path: Path, capsys: Any) -> None:
    code, out, _ = _run(
        _store(tmp_path), capsys, "--since", "2026-10-01", "--until", "2026-10-07", "--json"
    )

    assert code == 1
    assert json.loads(out)["empty"] is True


def test_a_missing_store_is_not_found(tmp_path: Path, capsys: Any) -> None:
    code, _, err = _run(tmp_path / "missing.duckdb", capsys)

    assert code == ExitCode.NOT_FOUND
    assert "factory_report:" in err


def test_a_missing_store_prints_an_envelope_under_json(tmp_path: Path, capsys: Any) -> None:
    code, out, err = _run(tmp_path / "missing.duckdb", capsys, "--json")

    assert code == ExitCode.NOT_FOUND
    assert out == ""
    assert isinstance(json.loads(err), dict)


@pytest.mark.parametrize(
    "args",
    [
        ("--since", "yesterday"),
        ("--until", "2026-13-01"),
        ("--since", "2026-11-20", "--until", "2026-11-10"),
    ],
)
def test_bad_dates_are_a_usage_error(tmp_path: Path, capsys: Any, args: tuple[str, ...]) -> None:
    code, _, err = _run(_store(tmp_path), capsys, *args)

    assert code == ExitCode.USAGE
    assert "factory_report:" in err


def test_a_manifest_without_scoring_is_a_usage_error(tmp_path: Path, capsys: Any) -> None:
    manifest = yaml.safe_load(JUDGE_MANIFEST.read_text(encoding="utf-8"))
    manifest.pop("scoring")
    bare = tmp_path / "agent.yaml"
    bare.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    code, _, err = _run(_store(tmp_path), capsys, manifest=bare)

    assert code in (ExitCode.USAGE, ExitCode.AUTH_OR_CONFIG)
    assert "factory_report:" in err


def test_a_missing_manifest_is_an_error(tmp_path: Path, capsys: Any) -> None:
    code, _, err = _run(_store(tmp_path), capsys, manifest=tmp_path / "nope.yaml")

    assert code != 0
    assert "factory_report:" in err
