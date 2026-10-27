"""Tests for scripts/calibrate.py, with `gh` and the Jev runner replaced by fakes: no network."""

import datetime
import importlib.util
import json
import uuid
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from evals.calibration.fetch import FetchError
from factory.exit import AuthOrConfigError, ExitCode
from registry import JevOutput, JevSpec
from runtime.budget import Budget
from runtime.trace import Trace, TraceRow

SCRIPT = Path(__file__).parents[2] / "scripts" / "calibrate.py"
TODAY = datetime.date(2026, 10, 27)
MARKER = "UNTRUSTED-DIFF-CONTENT"
"""Every fake diff contains this, so a test can check that no output ever quotes a diff."""
JEV_COST = 0.002


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("calibrate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def _sha(n: int) -> str:
    return f"{n:040x}"


def _labels(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """Write a labels file of `(id, label)` rows, each with its own commit pair."""
    diffs = [
        {
            "id": id,
            "repo": "https://github.com/acme/shop",
            "base": _sha(2 * n + 1),
            "head": _sha(2 * n + 2),
            "private": True,
            "label": label,
            "severity": "major" if label == "risky" else "none",
            "reason": "A test row.",
            "source": "reader",
            "labelled_by": "tester",
            "labelled_on": "2026-10-27",
        }
        for n, (id, label) in enumerate(rows)
    ]
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"diffs": diffs}), encoding="utf-8")
    return path


ROWS = [("shop#1", "risky"), ("shop#2", "risky"), ("shop#3", "safe"), ("shop#4", "safe")]


class FakeGh:
    """A fake `fetch_diff`: each commit pair's diff names its head and contains `MARKER`."""

    def __init__(self, sizes: dict[str, int] | None = None, fail: str | None = None) -> None:
        self.sizes = sizes or {}
        self.fail = fail

    def __call__(self, owner_repo: str, base: str, head: str) -> str:
        if self.fail is not None:
            raise FetchError(f"{owner_repo}: {self.fail}")
        diff = f"diff --git a/{head}.py b/{head}.py\n+{MARKER}\n"
        return diff + "+x\n" * self.sizes.get(head, 0)


class FakeJev:
    """A fake `run_jev` that answers by diff and writes a trace row with a cost, as run_jev does.

    `answers` maps a head SHA to the `needs_review` probability, or to an exception to raise.
    Heads in `truncate` are reported as cut to fit, through the row's `decision_json`.
    """

    def __init__(
        self, answers: dict[str, float | Exception], truncate: frozenset[str] = frozenset()
    ) -> None:
        self.answers = answers
        self.truncate = truncate
        self.calls = 0

    def __call__(
        self,
        spec: JevSpec,
        payload: dict[str, str],
        trace: Trace,
        *,
        pipeline_run_id: str,
        budget: Budget | None,
    ) -> JevOutput:
        self.calls += 1
        assert pipeline_run_id.startswith(script.RUN_PREFIX)
        head = next(h for h in self.answers if h in payload["diff"])
        answer = self.answers[head]
        trace.record(
            TraceRow(
                run_id=str(uuid.uuid4()),
                agent_ref=spec.address,
                kind="jev",
                started_at=datetime.datetime.now(datetime.UTC),
                duration_ms=1,
                input_json={},
                pipeline_run_id=pipeline_run_id,
                cost_usd=JEV_COST,
                decision_json={"truncated": head in self.truncate},
            )
        )
        if budget is not None:
            budget.charge(JEV_COST)
        if isinstance(answer, Exception):
            raise answer
        return spec.output_class.model_validate(
            {
                "needs_review": {"p": answer},
                "reviewers": {
                    "answer": "code",
                    "probs": {"code": 0.91, "code-security": 0.03, "code-perf": 0.03, "all": 0.03},
                    "confidence": 0.91,
                },
            }
        )


def _answers(*ps: float | Exception) -> dict[str, float | Exception]:
    """Answers for `ROWS`, in order: row n has head `_sha(2 * n + 2)`."""
    return {_sha(2 * n + 2): p for n, p in enumerate(ps)}


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    jev: FakeJev,
    *args: str,
    rows: list[tuple[str, str]] = ROWS,
    gh: FakeGh | None = None,
) -> tuple[int, str, str]:
    argv = ["--labels", str(_labels(tmp_path, rows)), "--trace", str(tmp_path / "t.duckdb")]
    code = script.main([*argv, *args], fetch=gh or FakeGh(), jev_runner=jev, today=TODAY)
    out, err = capsys.readouterr()
    assert MARKER not in out and MARKER not in err
    return code, out, err


def test_the_table_marks_the_threshold_that_skips_most_safe_diffs_at_full_recall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jev = FakeJev(_answers(0.9, 0.6, 0.01, 0.03))
    code, out, _ = _run(tmp_path, capsys, jev)
    assert code == ExitCode.OK
    assert jev.calls == 4
    (chosen,) = [line for line in out.splitlines() if line.startswith("* ")]
    assert chosen.split() == ["*", "0.05", "1.000", "2/2", "1.000", "2/2", "$0.0020", "-"]
    assert "skip gate: needs_review p_below <threshold>" in out
    assert "# Calibrated 2026-10-27 on 4 labelled diffs (2 risky)" in out
    assert "    value: 0.05" in out and "    question: needs_review" in out


def test_json_carries_the_sweep_the_cost_from_the_trace_and_the_manifest_snippet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [*ROWS, ("shop#5", "safe")]
    jev = FakeJev(_answers(0.9, 0.6, 0.01, 0.03, 0.01), truncate=frozenset({_sha(10)}))
    code, out, _ = _run(tmp_path, capsys, jev, "--json", "--panel-cost-usd", "0.5", rows=rows)
    assert code == ExitCode.OK
    result = json.loads(out)
    assert result["chosen"] == 0.05
    assert result["diffs"] == 5 and result["truncated"] == 1
    assert result["skip_ceiling_safe"] == pytest.approx(2 / 3)
    assert result["jev_cost_per_diff_usd"] == pytest.approx(JEV_COST)
    assert result["cost_includes_panel"] is True
    row = next(row for row in result["rows"] if row["threshold"] == 0.5)
    assert row["safe_skipped"] == 2  # the truncated safe diff is reviewed at every threshold
    assert row["cost_per_diff_usd"] == pytest.approx(JEV_COST + 3 / 5 * 0.5)
    assert "value: 0.05" in result["manifest_snippet"]


def test_no_threshold_reaching_the_recall_exits_1_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(tmp_path, capsys, FakeJev(_answers(0.9, 0.01, 0.01, 0.03)))
    assert code == ExitCode.ERROR
    assert "no threshold reaches recall 0.98 on risky diffs" in err
    assert not any(line.startswith("* ") for line in out.splitlines())
    assert "low_risk" not in out


def test_a_failed_triage_call_is_listed_and_the_sweep_exits_1_as_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [*ROWS, ("shop#5", "safe")]
    jev = FakeJev(_answers(0.9, 0.6, 0.01, 0.03, RuntimeError("bad answer\nsecond line")))
    code, out, err = _run(tmp_path, capsys, jev, rows=rows)
    assert code == ExitCode.ERROR
    assert "failed, not measured: shop#5" in out
    assert "shop#5: triage failed (RuntimeError: bad answer)" in err
    assert "second line" not in err
    assert "sweep incomplete: 1 of 5 triage calls failed" in err


def test_a_rejected_key_stops_at_the_first_call_with_exit_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jev = FakeJev(_answers(*[AuthOrConfigError("TYPESAFE_API_KEY was rejected")] * 4))
    code, out, err = _run(tmp_path, capsys, jev, "--json")
    assert code == ExitCode.AUTH_OR_CONFIG
    assert jev.calls == 1
    assert out == ""
    assert json.loads(err.splitlines()[-1])["error"]["code"] == ExitCode.AUTH_OR_CONFIG


def test_the_budget_stops_the_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    jev = FakeJev(_answers(0.9, 0.6, 0.01, 0.03))
    code, _, err = _run(tmp_path, capsys, jev, "--budget-usd", "0.003")
    assert code == ExitCode.ERROR
    assert jev.calls == 2
    assert "budget" in err.lower()


def test_dry_run_reports_sizes_without_calling_jev(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jev = FakeJev({})
    gh = FakeGh(sizes={_sha(8): 9000})
    code, out, _ = _run(tmp_path, capsys, jev, "--dry-run", gh=gh)
    assert code == ExitCode.OK
    assert jev.calls == 0
    assert "4 diffs fetched; 1 exceed max_chars 24000" in out
    assert "1 of them among 2 safe diffs" in out
    code, out, _ = _run(tmp_path, capsys, jev, "--dry-run", "--json", "--limit", "3", gh=gh)
    report = json.loads(out)
    assert code == ExitCode.OK
    assert report["diffs"] == 3 and report["truncated"] == 0
    assert [row["id"] for row in report["rows"]] == ["shop#1", "shop#2", "shop#3"]


def test_a_failed_fetch_exits_1_before_any_jev_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jev = FakeJev({})
    code, _, err = _run(tmp_path, capsys, jev, gh=FakeGh(fail="HTTP 404"))
    assert code == ExitCode.ERROR
    assert jev.calls == 0
    assert "could not fetch 4 of 4 diffs" in err


@pytest.mark.parametrize(
    "args",
    [["--thresholds", "0.1,abc"], ["--thresholds", "0,0.1"], ["--limit", "0"]],
)
def test_bad_arguments_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    jev = FakeJev(_answers(0.9, 0.6, 0.01, 0.03))
    code, _, err = _run(tmp_path, capsys, jev, *args)
    assert code == ExitCode.USAGE
    assert err.startswith("calibrate: ")


def test_a_manifest_that_is_not_jev_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = Path(__file__).parents[2] / "agents" / "code-reviewer" / "agent.yaml"
    code, _, err = _run(tmp_path, capsys, FakeJev({}), "--manifest", str(manifest))
    assert code == ExitCode.USAGE
    assert "is not a Jev manifest" in err


def test_the_question_flag_takes_an_option(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jev = FakeJev(_answers(0.9, 0.6, 0.01, 0.03))
    code, out, _ = _run(tmp_path, capsys, jev, "--json", "--question", "reviewers=all")
    result = json.loads(out)
    assert code == ExitCode.OK
    assert result["question"] == "reviewers=all"
    # probs["all"] is 0.03 for every diff, so only 0.02 keeps the risky ones, and it skips nothing.
    assert result["chosen"] == 0.02
    assert result["rows"][0]["safe_skipped"] == 0
    assert "    question: reviewers\n    option: all\n" in result["manifest_snippet"]
