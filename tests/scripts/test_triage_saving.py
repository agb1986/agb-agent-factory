"""Tests for scripts/triage_saving.py, with fake runners in place of Claude and Jev."""

import importlib.util
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from factory.exit import ExitCode
from pipelines.review_panel import JUDGE, REVIEWERS
from registry.jev_spec import JevOutput, JevSpec
from registry.spec import LLMSpec
from runtime.trace import Trace, TraceRow

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "triage_saving.py"
MARKER = "UNTRUSTED-DIFF-CONTENT"
"""Every fake finding contains this, so a test can check none is printed."""
REVIEWER_USD = 0.25
JUDGE_USD = 0.5
JEV_USD = 0.01
PANEL_USD = len(REVIEWERS) * REVIEWER_USD + JUDGE_USD
"""What the reviewers and the judge cost in one fake panel run."""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_saving", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def _fixture_and_mode(pipeline_run_id: str) -> tuple[str, str]:
    """Split `triage-saving-<token>-<fixture>-<mode>` into the fixture and the mode."""
    rest = pipeline_run_id.removeprefix(script.RUN_PREFIX).partition("-")[2]
    for mode in ("no-triage", "triage"):  # longest first: "-triage" ends "-no-triage" too
        if rest.endswith(f"-{mode}"):
            return rest.removesuffix(f"-{mode}"), mode
    raise AssertionError(pipeline_run_id)


def _finding(severity: str) -> dict[str, Any]:
    return {
        "file": f"{MARKER}.py",
        "line_start": 1,
        "line_end": 1,
        "severity": severity,
        "category": "logic",
        "title": f"{MARKER} title",
        "body": f"{MARKER} body",
    }


class FakeRunner:
    """Stands in for `runtime.runner.run`: one finding of the fixture's severity per reviewer.

    `failing` names `(fixture, mode)` pairs whose every call raises, with untrusted text in the
    message so a test can check the message is not printed.
    """

    def __init__(
        self, severities: Mapping[str, str], failing: set[tuple[str, str]] | None = None
    ) -> None:
        self.severities = dict(severities)
        self.failing = failing or set()
        self.calls: list[str] = []

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        run_id = kwargs["pipeline_run_id"]
        self.calls.append(run_id)
        fixture, mode = _fixture_and_mode(run_id)
        if (fixture, mode) in self.failing:
            raise RuntimeError(f"{MARKER} failure")
        findings = [_finding(self.severities[fixture])] if fixture in self.severities else []
        if spec.name == JUDGE[0]:
            output: dict[str, Any] = {
                "scores": {
                    name: {"value": 0.5, "justification": "Checked."}
                    for name in (
                        "correctness_risk",
                        "security_risk",
                        "test_coverage",
                        "maintainability",
                    )
                },
                "merged_findings": [
                    finding | {"sources": [f"{REVIEWERS[0][0]}@1.0.0"]} for finding in findings
                ],
                "rationale": f"{MARKER} rationale",
            }
        else:
            verdict = "request_changes" if findings else "approve"
            output = {"findings": findings, "summary": f"{MARKER} summary", "verdict": verdict}
        trace.record(
            TraceRow(
                run_id=str(uuid.uuid4()),
                pipeline_run_id=run_id,
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=dict(payload),
                cost_usd=JUDGE_USD if spec.name == JUDGE[0] else REVIEWER_USD,
                output_json=output,
            )
        )
        return spec.output_class.model_validate(output)


class FakeJev:
    """Stands in for `runtime.jev_runner.run_jev`: answers `needs_review` with the fixture's `p`."""

    def __init__(self, p: Mapping[str, float], *, error: bool = False) -> None:
        self.p = dict(p)
        self.error = error
        self.calls: list[str] = []

    def __call__(
        self, spec: JevSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> JevOutput:
        run_id = kwargs["pipeline_run_id"]
        self.calls.append(run_id)
        trace.record(
            TraceRow(
                run_id=str(uuid.uuid4()),
                pipeline_run_id=run_id,
                agent_ref=spec.address,
                kind="jev",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=dict(payload),
                cost_usd=JEV_USD,
            )
        )
        if self.error:
            raise RuntimeError(f"{MARKER} jev failure")
        fixture, _ = _fixture_and_mode(run_id)
        return spec.output_class.model_validate(
            {
                "needs_review": {"p": self.p[fixture]},
                "reviewers": {
                    "answer": "all",
                    "probs": {"code": 0.1, "code-security": 0.1, "code-perf": 0.1, "all": 0.7},
                    "confidence": 0.9,
                },
            }
        )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build fixtures under `tmp_path`, set dummy keys, and return the trace store's path."""
    monkeypatch.setattr(script, "REPOS", tmp_path / "repos")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy")
    return tmp_path / "traces.duckdb"


def _main(
    db: Path, *args: str, runner: FakeRunner | None = None, jev: FakeJev | None = None
) -> int:
    return script.main(
        ["--trace-db", str(db), "--allow-unpublished", *args],
        runner=runner or FakeRunner({}),
        jev_runner=jev or FakeJev({}),
    )


def _document(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out, err = capsys.readouterr()
    assert MARKER not in out + err
    return json.loads(out)


SKIP_AND_REVIEW = ["--fixtures", "docs-only,mixed"]
"""Two fixtures: `docs-only`, which the fakes skip, and `mixed`, which they review."""


def test_the_totals_add_up(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner({"docs-only": "minor", "mixed": "major"})
    jev = FakeJev({"docs-only": 0.1, "mixed": 0.9})

    code = _main(env, *SKIP_AND_REVIEW, "--json", runner=runner, jev=jev)

    assert code == ExitCode.OK
    document = _document(capsys)
    docs, mixed = document["fixtures"]
    assert docs["fixture"] == "docs-only"
    assert docs["decision"]["skip"] is True
    assert docs["severity"] == "minor"
    assert mixed["decision"]["skip"] is False
    assert mixed["severity"] == "major"
    assert docs["with_triage"]["cost_usd"] == pytest.approx(PANEL_USD + JEV_USD)
    assert docs["no_triage"]["cost_usd"] == pytest.approx(PANEL_USD)
    assert docs["jev_cost_usd"] == pytest.approx(JEV_USD)
    assert document["totals"] == pytest.approx(
        {
            "fixtures": 2,
            "with_triage_usd": 2 * (PANEL_USD + JEV_USD),
            "no_triage_usd": 2 * PANEL_USD,
            "difference_usd": 2 * JEV_USD,
            "jev_overhead_usd": 2 * JEV_USD,
            "would_skip": 1,
            "projected_saving_usd": PANEL_USD - 2 * JEV_USD,
            "would_skip_major": 0,
            "measurement_usd": 4 * PANEL_USD + 2 * JEV_USD,
        }
    )
    assert len(jev.calls) == 2  # triage runs once per fixture, only in the with-triage run


def test_the_json_document_has_its_shape(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _main(env, "--fixtures", "mixed", "--json", jev=FakeJev({"mixed": 0.9}))

    document = _document(capsys)
    assert set(document) == {
        "run", "agents", "skip_gate", "budget_usd", "max_usd", "fixtures", "totals", "failing",
        "stopped",
    }  # fmt: skip
    assert document["skip_gate"] == {"question": "needs_review", "threshold": 0.5}
    assert document["agents"]["triage"].startswith("diff-triage@")
    assert len(document["agents"]["reviewers"]) == len(REVIEWERS)
    (fixture,) = document["fixtures"]
    assert set(fixture) == {
        "fixture", "with_triage", "no_triage", "jev_cost_usd", "decision", "severity",
    }  # fmt: skip
    run = document["run"]
    assert fixture["with_triage"]["pipeline_run_id"] == f"triage-saving-{run}-mixed-triage"
    assert fixture["no_triage"]["pipeline_run_id"] == f"triage-saving-{run}-mixed-no-triage"
    assert set(fixture["decision"]) == {"status", "skip", "escalate", "route", "reason"}
    assert fixture["severity"] is None
    assert document["failing"] == []
    assert document["stopped"] is None


def test_a_would_skip_fixture_with_a_major_finding_exits_1(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner({"docs-only": "major"})
    jev = FakeJev({"docs-only": 0.1, "mixed": 0.9})

    code = _main(env, *SKIP_AND_REVIEW, runner=runner, jev=jev)

    assert code == ExitCode.ERROR
    out, err = capsys.readouterr()
    assert MARKER not in out + err
    assert "FAIL: would-skip fixtures with a major finding or worse: docs-only" in out
    assert "1 would-skip fixtures have a major finding" in err


def test_a_major_finding_on_a_reviewed_fixture_passes(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner({"mixed": "blocker"})
    jev = FakeJev({"docs-only": 0.1, "mixed": 0.9})

    assert _main(env, *SKIP_AND_REVIEW, runner=runner, jev=jev) == ExitCode.OK
    out, _ = capsys.readouterr()
    assert "OK: no would-skip fixture" in out


def test_a_failed_run_is_reported_by_type_and_exits_1(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner({}, failing={("mixed", "no-triage")})
    jev = FakeJev({"docs-only": 0.1, "mixed": 0.9})

    code = _main(env, *SKIP_AND_REVIEW, "--json", runner=runner, jev=jev)

    assert code == ExitCode.ERROR
    document = _document(capsys)
    assert document["failing"] == ["mixed"]
    mixed = document["fixtures"][1]
    assert mixed["no_triage"]["error"] == "AllReviewersFailed"
    assert mixed["with_triage"]["error"] is None


def test_a_failed_triage_call_counts_as_a_failed_run(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _main(env, "--fixtures", "mixed", "--json", jev=FakeJev({}, error=True))

    assert code == ExitCode.ERROR
    document = _document(capsys)
    (mixed,) = document["fixtures"]
    assert mixed["decision"]["status"] == "jev_error"
    assert mixed["decision"]["skip"] is False
    assert mixed["with_triage"]["error"] == "jev_error"
    assert document["totals"]["would_skip"] == 0


def test_spend_past_max_usd_stops_before_the_next_fixture(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner({})
    jev = FakeJev({"docs-only": 0.1, "mixed": 0.9})

    code = _main(env, *SKIP_AND_REVIEW, "--max-usd", "1", "--json", runner=runner, jev=jev)

    assert code == ExitCode.ERROR
    document = _document(capsys)
    assert [fixture["fixture"] for fixture in document["fixtures"]] == ["docs-only"]
    assert document["stopped"].startswith("spent $")
    assert not any("-mixed-" in run_id for run_id in runner.calls)


def test_the_spend_is_projected_after_three_fixtures() -> None:
    assert script._over(1.0, 2, 10, 5.0) is None  # too few fixtures to project
    assert script._over(1.0, 3, 10, 5.0) is None  # 3.33 over all ten
    assert "projects to $6.0000 over 10" in script._over(1.8, 3, 10, 5.0)
    assert script._over(4.0, 3, 3, 5.0) is None  # nothing left to project
    assert script._over(5.5, 3, 3, 5.0).startswith("spent $5.5000")


def test_text_output_prints_no_untrusted_content(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner({"docs-only": "minor", "mixed": "major"})
    jev = FakeJev({"docs-only": 0.1, "mixed": 0.9})

    assert _main(env, *SKIP_AND_REVIEW, runner=runner, jev=jev) == ExitCode.OK
    out, err = capsys.readouterr()
    assert MARKER not in out + err
    assert "1 would skip; projected saving" in out
    assert "docs-only" in out and "mixed" in out


def test_a_dry_run_builds_and_calls_nothing(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    runner, jev = FakeRunner({}), FakeJev({})

    code = _main(env, *SKIP_AND_REVIEW, "--dry-run", "--json", runner=runner, jev=jev)

    assert code == ExitCode.OK
    document = _document(capsys)
    assert document["dry_run"] is True
    assert document["fixtures"] == [
        {"fixture": "docs-only", "built": False},
        {"fixture": "mixed", "built": False},
    ]
    assert runner.calls == [] and jev.calls == []
    assert not script.REPOS.exists()
    assert not env.exists()


def test_a_dry_run_measures_every_fixture_by_default(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fixtures.build import discover

    assert _main(env, "--dry-run") == ExitCode.OK
    out, _ = capsys.readouterr()
    assert f"{len(discover())} fixtures, 2 panel runs each" in out


def test_an_unknown_fixture_exits_3(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _main(env, "--fixtures", "mixed,nope", "--dry-run") == ExitCode.NOT_FOUND
    _, err = capsys.readouterr()
    assert "no fixture named 'nope'" in err


def test_a_missing_key_exits_4_before_any_call(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY")
    runner, jev = FakeRunner({}), FakeJev({})

    code = _main(env, "--fixtures", "mixed", "--json", runner=runner, jev=jev)

    assert code == ExitCode.AUTH_OR_CONFIG
    _, err = capsys.readouterr()
    assert json.loads(err)["error"]["message"] == "TYPESAFE_API_KEY is not set"
    assert runner.calls == [] and jev.calls == []


@pytest.mark.parametrize("flag", ["--budget-usd", "--max-usd"])
def test_a_budget_of_zero_is_a_usage_error(env: Path, flag: str) -> None:
    assert _main(env, flag, "0", "--dry-run") == ExitCode.USAGE
