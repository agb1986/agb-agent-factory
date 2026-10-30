"""Tests for scripts/shadow_report.py, over a trace store written with `Trace.record`."""

import datetime
import importlib.util
import json
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest
import yaml

from factory.exit import ExitCode
from pipelines.review_panel import TRIAGE_TAG
from runtime.trace import Trace, TraceRow

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "shadow_report.py"
MANIFEST = ROOT / "agents" / "diff-triage" / "agent.yaml"
TODAY = datetime.date(2026, 10, 27)
MARKER = "UNTRUSTED-DIFF-CONTENT"
"""Every fake diff, file name and finding contains this, so a test can check none is printed."""
TRIAGE = f"diff-triage@{yaml.safe_load(MANIFEST.read_text(encoding='utf-8'))['version']}"
REVIEWER = "code-reviewer@0.1.0"
JUDGE = "review-judge@0.1.0"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shadow_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def _gated_manifest(tmp_path: Path, value: float | None = 0.05) -> Path:
    """Write a copy of the diff-triage manifest whose `low_risk` skip gate is at `value`.

    `None` writes the copy with no skip gate at all.
    """
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    manifest["gates"] = [gate for gate in manifest["gates"] if gate["action"] != "skip"]
    if value is None:
        for gate in manifest["gates"]:
            gate.pop("unless", None)
    else:
        manifest["gates"].append(
            {
                "name": "low_risk",
                "question": "needs_review",
                "rule": "p_below",
                "value": value,
                "action": "skip",
            }
        )
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path


def _answers(p: float, all_p: float = 0.03) -> dict[str, Any]:
    rest = (1 - 0.9 - all_p) / 2
    return {
        "needs_review": {"p": p},
        "reviewers": {
            "answer": "code",
            "probs": {"code": 0.9, "code-security": rest, "code-perf": rest, "all": all_p},
            "confidence": 0.9,
        },
    }


class Store:
    """A trace store in `tmp_path` with helpers that write the rows one panel run leaves."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "traces.duckdb"

    def _row(self, run: str, when: datetime.datetime, **fields: Any) -> None:
        with Trace(self.path) as trace:
            trace.record(
                TraceRow(
                    run_id=str(uuid.uuid4()),
                    started_at=when,
                    duration_ms=1,
                    pipeline_run_id=run,
                    **fields,
                )
            )

    def panel_run(
        self,
        run: str,
        p: float | None,
        *,
        day: datetime.date = TODAY,
        severities: tuple[str, ...] = (),
        judge: tuple[str, ...] = (),
        truncated: bool = False,
        error: str | None = None,
        output: dict[str, Any] | None = None,
        reviewed: bool = True,
        agent_ref: str = TRIAGE,
    ) -> None:
        """Write a triage decision, the Jev run under it and, when `reviewed`, the panel's rows.

        `p` is the `needs_review` probability, or `None` for a run without answers. The Jev run
        costs 0.001, each reviewer 0.1 and the judge 0.05.
        """
        when = datetime.datetime.combine(day, datetime.time(9), datetime.UTC)
        files = [f"src/{MARKER}.py"]
        self._row(
            run,
            when,
            agent_ref=agent_ref,
            kind="jev",
            input_json={"diff": f"+{MARKER}\n", "files": files},
            cost_usd=0.001,
        )
        answers = output if output is not None else (None if p is None else _answers(p))
        self._row(
            run,
            when,
            agent_ref=agent_ref,
            kind="jev",
            input_json={"files": files},
            cost_usd=0.0,
            decision_json={"skip": False, "route_only": True, "truncated": truncated},
            output_json=answers,
            error=error,
            tag=TRIAGE_TAG,
        )
        if not reviewed:
            return
        finding = {"file": files[0], "title": MARKER, "body": MARKER}
        self._row(
            run,
            when,
            agent_ref=REVIEWER,
            kind="llm",
            input_json={"repo_path": "/repo", "base": "b" * 40, "head": "c" * 40},
            cost_usd=0.1,
            output_json={
                "findings": [{**finding, "severity": s} for s in severities],
                "summary": MARKER,
                "verdict": "approve",
            },
        )
        self._row(
            run,
            when,
            agent_ref=JUDGE,
            kind="llm",
            input_json={"reviews": MARKER},
            cost_usd=0.05,
            output_json={"merged_findings": [{**finding, "severity": s} for s in judge]},
        )


def _run(
    store: Store, capsys: pytest.CaptureFixture[str], *args: str, manifest: Path | None = None
) -> tuple[int, str, str]:
    argv = ["--trace", str(store.path)]
    if manifest is not None:
        argv += ["--manifest", str(manifest)]
    code = script.main([*argv, *args], today=TODAY)
    out, err = capsys.readouterr()
    assert MARKER not in out and MARKER not in err
    return code, out, err


def test_a_would_skip_diff_with_a_blocker_fails_and_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("run-safe", 0.01, severities=("minor",))
    store.panel_run("run-bad", 0.02, judge=("blocker",))
    store.panel_run("run-risky", 0.9, severities=("blocker",))
    code, out, err = _run(store, capsys, manifest=_gated_manifest(tmp_path))
    assert code == ExitCode.ERROR
    assert "3 triaged runs: 2 would skip (66.7%), 1 reviewed" in out
    assert "skip gate needs_review p_below 0.05" in out
    assert "FAIL: would-skip runs with a blocker finding or worse: run-bad" in out
    (line,) = [line for line in out.splitlines() if line.endswith("  run-bad")]
    assert line.split()[2:5] == ["0.020", "blocker", "$0.1510"]
    assert "1 would-skip runs have a blocker finding or worse" in err


def test_json_reports_the_rates_the_cost_and_each_would_skip_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("run-a", 0.01, severities=("minor", "info"))
    store.panel_run("run-b", 0.5, severities=("major",))
    code, out, _ = _run(store, capsys, "--json", manifest=_gated_manifest(tmp_path))
    assert code == ExitCode.OK
    report = json.loads(out)
    assert report["since"] == "2026-10-14" and report["until"] == "2026-10-27"
    assert report["agent"] == TRIAGE and report["threshold"] == 0.05
    assert report["runs"] == 2 and report["skip_rate"] == 0.5
    assert report["counts"] == {
        "skip": 1,
        "review": 1,
        "truncated": 0,
        "jev_error": 0,
        "unreadable": 0,
    }
    assert report["panel_runs"] == 2
    assert report["panel_cost_per_run_usd"] == pytest.approx(0.15)
    assert report["saved_usd"] == pytest.approx(0.15)
    (run,) = report["would_skip"]
    assert run["pipeline_run_id"] == "run-a"
    assert run["p"] == 0.01 and run["severity"] == "minor"
    assert run["base"] == "b" * 40 and run["head"] == "c" * 40
    assert run["cost_usd"] == pytest.approx(0.151)
    assert run["triage_cost_usd"] == pytest.approx(0.001)
    assert report["failing"] == [] and report["unchecked"] == []


def test_truncated_failed_and_unreadable_rows_never_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("run-cut", 0.01, truncated=True, severities=("blocker",))
    store.panel_run("run-err", None, error="jev failed", severities=("blocker",))
    store.panel_run("run-odd", None, output={"needs_review": {"p": 0.01}}, judge=("blocker",))
    code, out, _ = _run(store, capsys, "--json", manifest=_gated_manifest(tmp_path))
    report = json.loads(out)
    assert code == ExitCode.OK
    assert report["counts"] == {
        "skip": 0,
        "review": 0,
        "truncated": 1,
        "jev_error": 1,
        "unreadable": 1,
    }
    assert report["would_skip"] == [] and report["skip_rate"] == 0.0
    code, out, _ = _run(store, capsys, manifest=_gated_manifest(tmp_path))
    assert "1 truncated, 1 jev_error, 1 unreadable" in out
    assert "OK: no would-skip run has a blocker finding or worse" in out


def test_calibration_runs_other_agents_and_days_out_of_range_are_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("calibrate-1234-shop#1", 0.01, judge=("blocker",))
    store.panel_run("run-other", 0.01, agent_ref="other-triage@0.1.0", judge=("blocker",))
    store.panel_run("run-old", 0.01, day=datetime.date(2026, 10, 13), judge=("blocker",))
    store.panel_run("run-new", 0.01, day=datetime.date(2026, 10, 28), judge=("blocker",))
    store.panel_run("run-first", 0.01, day=datetime.date(2026, 10, 14))
    code, out, _ = _run(store, capsys, "--json", manifest=_gated_manifest(tmp_path))
    report = json.loads(out)
    assert code == ExitCode.OK
    assert [run["pipeline_run_id"] for run in report["would_skip"]] == ["run-first"]
    code, out, _ = _run(
        store,
        capsys,
        "--json",
        "--since",
        "2026-10-13",
        "--until",
        "2026-10-13",
        manifest=_gated_manifest(tmp_path),
    )
    assert code == ExitCode.ERROR
    assert json.loads(out)["failing"] == ["run-old"]


def test_fail_on_major_and_a_run_without_panel_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("run-major", 0.01, severities=("major",))
    store.panel_run("run-bare", 0.01, reviewed=False)
    manifest = _gated_manifest(tmp_path)
    code, out, _ = _run(store, capsys, manifest=manifest)
    assert code == ExitCode.OK
    assert "1 would-skip runs have no panel output to check" in out
    (bare,) = [line for line in out.splitlines() if line.endswith("  run-bare")]
    assert "no panel" in bare
    code, out, _ = _run(store, capsys, "--json", "--fail-on", "major", manifest=manifest)
    report = json.loads(out)
    assert code == ExitCode.ERROR
    assert report["failing"] == ["run-major"] and report["unchecked"] == ["run-bare"]
    assert report["panel_runs"] == 1


def test_threshold_and_question_flags_move_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("run-a", 0.08)
    store.panel_run("run-b", 0.2)
    code, out, _ = _run(store, capsys, "--json", "--threshold", "0.1")
    report = json.loads(out)
    assert code == ExitCode.OK
    assert report["question"] == "needs_review" and report["threshold"] == 0.1
    assert [run["pipeline_run_id"] for run in report["would_skip"]] == ["run-a"]
    args = ("--json", "--threshold", "0.05", "--question", "reviewers=all")
    code, out, _ = _run(store, capsys, *args)
    report = json.loads(out)
    assert report["question"] == "reviewers=all"
    assert [run["p"] for run in report["would_skip"]] == [0.03, 0.03]


def test_an_empty_range_reports_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(tmp_path)
    store.panel_run("run-old", 0.01, day=datetime.date(2026, 9, 1))
    code, out, _ = _run(store, capsys, manifest=_gated_manifest(tmp_path))
    assert code == ExitCode.OK
    assert "0 triaged runs: 0 would skip," in out
    assert "no panel output in range" in out
    code, out, _ = _run(store, capsys, "--json", manifest=_gated_manifest(tmp_path))
    report = json.loads(out)
    assert report["skip_rate"] is None and report["panel_cost_per_run_usd"] is None


@pytest.mark.parametrize(
    "args",
    [
        ["--threshold", "0"],
        ["--threshold", "0.1", "--question", "nope"],
        ["--threshold", "0.1", "--since", "2026-13-01"],
        ["--threshold", "0.1", "--since", "2026-10-28"],
    ],
)
def test_bad_arguments_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("run-a", 0.01)
    code, _, err = _run(store, capsys, *args)
    assert code == ExitCode.USAGE
    assert err.startswith("shadow_report: ")


def test_a_manifest_without_a_skip_gate_needs_a_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    store.panel_run("run-a", 0.01)
    code, _, err = _run(store, capsys, manifest=_gated_manifest(tmp_path, None))
    assert code == ExitCode.USAGE
    assert "has no skip gate" in err


def test_a_manifest_that_is_not_jev_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    manifest = ROOT / "agents" / "code-reviewer" / "agent.yaml"
    code, _, err = _run(store, capsys, "--threshold", "0.1", manifest=manifest)
    assert code == ExitCode.USAGE
    assert "is not a Jev manifest" in err


def test_a_missing_store_exits_3_and_one_without_traces_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(tmp_path)
    code, _, err = _run(store, capsys, "--threshold", "0.1", "--json")
    assert code == ExitCode.NOT_FOUND
    assert json.loads(err)["error"]["code"] == ExitCode.NOT_FOUND
    duckdb.connect(str(store.path)).close()
    code, _, err = _run(store, capsys, "--threshold", "0.1")
    assert code == ExitCode.NOT_FOUND
    assert "has no traces table" in err
