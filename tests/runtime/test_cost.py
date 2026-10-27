"""Tests for runtime/cost.py and the `factory cost` subcommand."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory.cli import main
from factory.exit import ExitCode, UsageError
from runtime.cost import parse_since, report, text
from runtime.trace import ENV_VAR, CostRow, Trace, TraceRow

GOLDEN = Path(__file__).parent / "golden"
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
SINCE = "2026-10-20"

BASE = TraceRow(
    run_id="",
    agent_ref="code-reviewer@1.2.0",
    kind="llm",
    started_at=T0,
    duration_ms=900,
    input_json={},
)


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A trace file with a day of work: two pipelines, an eval run, a publish run, a loose Jev run."""
    path = tmp_path / "trace.duckdb"
    runs = [
        ("r1", "code-reviewer@1.2.0", "pipe-1", 0.012345, None),
        ("r2", "perf-reviewer@0.3.0", "pipe-1", 0.0231, None),
        ("r3", "judge@1.0.0", "pipe-1", 0.004, None),
        ("r4", "code-reviewer@1.2.0", "pipe-2", 0.0119, None),
        ("r5", "code-reviewer@1.2.0", None, 0.3, "eval:code-reviewer@1.2.0"),
        ("r6", "code-reviewer@1.2.0", None, 0.2, "eval:code-reviewer@1.2.0"),
        ("r7", "fixer@0.1.0", None, 0.45, "publish:fixer@0.1.0"),
        ("r8", "diff-triage@0.1.0", None, None, None),
    ]
    with Trace(path) as trace:
        for run_id, agent_ref, pipeline, cost, tag in runs:
            kind = "jev" if agent_ref.startswith("diff-triage@") else "llm"
            trace.record(
                replace(
                    BASE,
                    run_id=run_id,
                    agent_ref=agent_ref,
                    kind=kind,
                    pipeline_run_id=pipeline,
                    cost_usd=cost,
                    tag=tag,
                    started_at=T0 + timedelta(minutes=int(run_id[1:])),
                )
            )
        trace.record(
            replace(BASE, run_id="before", cost_usd=99.0, started_at=T0 - timedelta(days=1))
        )
    monkeypatch.setenv(ENV_VAR, str(path))
    return path


def test_the_json_by_agent_matches_the_golden_file(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cost", "--since", SINCE, "--json"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert json.loads(out) == json.loads((GOLDEN / "cost-by-agent.json").read_text())


def test_the_json_by_pipeline_matches_the_golden_file(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cost", "--since", SINCE, "--by", "pipeline", "--json"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert json.loads(out) == json.loads((GOLDEN / "cost-by-pipeline.json").read_text())


def test_the_json_by_kind_matches_the_golden_file(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cost", "--since", SINCE, "--by", "kind", "--json"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert json.loads(out) == json.loads((GOLDEN / "cost-by-kind.json").read_text())


def test_by_kind_names_llm_runs_claude(seeded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["cost", "--since", SINCE, "--by", "kind"])
    out = capsys.readouterr().out
    assert "  claude  " in out
    assert "  jev  " in out
    assert "llm" not in out


def test_the_text_output_matches_the_golden_file(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cost", "--since", SINCE]) == ExitCode.OK
    assert capsys.readouterr().out == (GOLDEN / "cost.txt").read_text()


def test_json_output_is_one_line_on_stdout(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["cost", "--since", SINCE, "--json"])
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert captured.err == ""


def test_an_empty_window_reports_zero(seeded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cost", "--since", "2027-01-01"]) == ExitCode.OK
    assert (
        capsys.readouterr().out
        == "cost since 2027-01-01T00:00:00Z, by agent\n\ntotal: 0 runs, $0.0000\n"
    )


def test_a_bad_since_is_a_usage_error_on_stderr(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cost", "--since", "last week"]) == ExitCode.USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a date" in captured.err


def test_since_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cost"]) == ExitCode.USAGE


def test_by_accepts_only_agent_pipeline_and_kind(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cost", "--since", SINCE, "--by", "model"]) == ExitCode.USAGE


def test_a_datetime_with_an_offset_moves_the_window(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["cost", "--since", "2026-10-20T11:00:00+02:00", "--json"])
    first = json.loads(capsys.readouterr().out)
    assert first["since"] == "2026-10-20T09:00:00Z"
    assert first["total_runs"] == 8


def test_parse_since_reads_a_date_as_midnight_utc() -> None:
    assert parse_since("2026-10-01") == datetime(2026, 10, 1, tzinfo=UTC)


def test_parse_since_keeps_an_explicit_offset() -> None:
    parsed = parse_since("2026-10-01T09:30:00+02:00")
    assert parsed == datetime(2026, 10, 1, 7, 30, tzinfo=UTC)


def test_parse_since_rejects_text() -> None:
    with pytest.raises(UsageError):
        parse_since("yesterday")


def test_report_names_a_missing_pipeline() -> None:
    document = report([CostRow(None, "other", 1, 0.5)], T0, "pipeline")
    assert document["buckets"]["other"]["groups"] == [
        {"pipeline_run_id": "(none)", "runs": 1, "cost_usd": 0.5}
    ]


def test_text_skips_empty_buckets() -> None:
    document = report([CostRow("a@1.0.0", "eval", 2, 0.25)], T0, "agent")
    lines = text(document).splitlines()
    assert not any(line.startswith("other runs") for line in lines)
    assert any(line.startswith("eval runs: 2 runs") for line in lines)
