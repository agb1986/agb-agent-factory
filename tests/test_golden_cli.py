"""Golden tests for the `--json` documents of the `factory` commands.

Each test runs a command through `factory.cli.main` with fakes in place of the model, parses the one
document on stdout, blanks the fields that change per run and compares it to `tests/golden/<name>.json`.
Refresh the files with `pytest tests/test_golden_cli.py --update-golden`; see `tests/golden/README.md`.
"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import evals.run as eval_run
from evals.cases import Case
from evals.graders.llm_judge import JudgeVerdict
from evals.run import EvalReport
from factory import cli
from factory.cli import main
from factory.exit import ExitCode
from fixtures.build import build
from pipelines import fix_loop, review_panel, triage
from registry import AgentRef
from registry.spec import LLMSpec
from runtime.trace import Trace, TraceRow
from tests.pipelines.test_fix_loop import FINDING, FakeSandbox, FixRunner
from tests.pipelines.test_review_panel import FakeJev

Golden = Callable[[str, Any], None]

# Fields that differ on every run: the value they are replaced with before comparing.
VOLATILE: dict[str, Any] = {
    "duration_ms": 0,
    "git_sha": "<git-sha>",
    "pipeline_run_id": "pipeline-run-1",
    "eval_run_id": "eval-run-1",
}
FIXTURES = ("off-by-one", "clean-refactor", "untested-change", "mixed")
SUMMARY = "A change."


def _normalised(value: Any) -> Any:
    """Return `value` with every volatile field, at any depth, replaced by its fixed stand-in."""
    if isinstance(value, dict):
        return {k: VOLATILE[k] if k in VOLATILE else _normalised(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalised(item) for item in value]
    return value


def _finding(file: str, start: int, end: int, severity: str) -> dict[str, Any]:
    return {
        "file": file,
        "line_start": start,
        "line_end": end,
        "severity": severity,
        "category": "logic",
        "title": "Problem",
        "body": "It is wrong.",
    }


# What a good code-reviewer answers for each of its fixtures.
ANSWERS: dict[str, dict[str, Any]] = {
    "off-by-one": {
        "verdict": "request_changes",
        "findings": [_finding("series/rolling.py", 12, 13, "major")],
    },
    "clean-refactor": {"verdict": "approve", "findings": []},
    "untested-change": {
        "verdict": "approve",
        "findings": [_finding("pricing/discount.py", 4, 8, "minor")],
    },
    "mixed": {
        "verdict": "request_changes",
        "findings": [
            _finding("shop/shipping.py", 12, 12, "major"),
            _finding("shop/format.py", 6, 7, "info"),
        ],
    },
}


def _run(argv: list[str], capsys: pytest.CaptureFixture[str], *, code: int = ExitCode.OK) -> Any:
    """Run `main(argv)`, check its exit code and return the normalised document on stdout."""
    assert main(argv) == code
    return _normalised(json.loads(capsys.readouterr().out))


# --- list


def test_list_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                   golden: Golden) -> None:  # fmt: skip
    agents = [
        AgentRef("alpha", "1.0.0", "llm"),
        AgentRef("alpha", "1.1.0", "llm"),
        AgentRef("beta", "0.3.0", "deterministic"),
    ]
    monkeypatch.setattr(cli, "list_agents", lambda: agents)
    golden("list", _run(["list", "--json"], capsys))


# --- eval and publish-check


@pytest.fixture
def fake_eval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the trace store at a scratch file and run every eval with a fake runner and judge."""
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    paths = {name: tmp_path / "repos" / name for name in FIXTURES}
    for path in paths.values():
        path.mkdir(parents=True)
    real = eval_run.evaluate

    def runner(spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any) -> BaseModel:
        answer = ANSWERS[Path(kwargs["repo_path"]).name]
        return spec.output_class.model_validate({"summary": SUMMARY} | answer)

    def judge(expected: Any, actual: Any) -> JudgeVerdict:
        return JudgeVerdict(passed=True, score=0.9, reason="judged")

    def fake(spec: LLMSpec, cases: list[Case], trace: Trace, **kwargs: Any) -> EvalReport:
        return real(
            spec,
            cases,
            trace,
            runner=runner,
            judge=judge,
            fixture_paths=paths,
            workers=kwargs["workers"],
            eval_run_id="eval-run-1",
        )

    monkeypatch.setattr(eval_run, "evaluate", fake)


@pytest.mark.usefixtures("fake_eval")
def test_eval_json(capsys: pytest.CaptureFixture[str], golden: Golden) -> None:
    golden("eval", _run(["eval", "code-reviewer@^1.2", "--json", "--workers", "2"], capsys))


@pytest.mark.usefixtures("fake_eval")
def test_publish_check_json(capsys: pytest.CaptureFixture[str], golden: Golden) -> None:
    golden("publish-check", _run(["publish-check", "code-reviewer@^1.2", "--json"], capsys))


# --- review


class FakeReviewRunner:
    """Stands in for `runtime.runner.run`: a fixed review per reviewer and a shipping judgement."""

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        is_judge = spec.name == "judge"
        trace.record(
            TraceRow(
                run_id=f"row-{spec.name}",
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=dict(payload),
                cost_usd=0.5 if is_judge else 0.25,
            )
        )
        if is_judge:
            scores = {
                "correctness_risk": 0.1,
                "security_risk": 0.0,
                "test_coverage": 0.8,
                "maintainability": 0.9,
            }
            return spec.output_class.model_validate(
                {
                    "scores": {
                        k: {"value": v, "justification": "Checked."} for k, v in scores.items()
                    },
                    "merged_findings": [
                        _finding("shop/shipping.py", 12, 12, "major")
                        | {"sources": ["code-reviewer@1.2.0"]}
                    ],
                    "rationale": "The comparison is flipped.",
                }
            )
        findings = (
            [_finding("shop/shipping.py", 12, 12, "major")] if spec.name == "code-reviewer" else []
        )
        verdict = "request_changes" if findings else "approve"
        return spec.output_class.model_validate(
            {"findings": findings, "summary": SUMMARY, "verdict": verdict}
        )


def test_review_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(review_panel, "run", FakeReviewRunner())
    monkeypatch.setattr(review_panel, "run_jev", FakeJev())
    (fixture,) = build(["mixed"], dest=tmp_path / "repos")
    argv = ["review", str(fixture.path), "--base", "base", "--head", "head"]
    golden("review", _run([*argv, "--allow-unpublished", "--json"], capsys))


# --- triage


def test_triage_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setattr(triage, "run_jev", FakeJev())
    (fixture,) = build(["mixed"], dest=tmp_path / "repos")
    argv = ["triage", str(fixture.path), "--base", "base", "--head", "head"]
    golden("triage", _run([*argv, "--allow-unpublished", "--json"], capsys))


# --- fix


def test_fix_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(fix_loop, "run", FixRunner())
    monkeypatch.setattr(fix_loop, "run_jev", FakeJev())
    monkeypatch.setattr(fix_loop, "DockerSandbox", FakeSandbox)
    (fixture,) = build(["off-by-one-red"], dest=tmp_path / "repos")
    out, work = tmp_path / "out", tmp_path / "work"
    work.mkdir()
    argv = ["fix", str(fixture.path), "--finding", json.dumps(FINDING), "--out-dir", str(out)]
    document = _run([*argv, "--work-dir", str(work), "--allow-unpublished", "--json"], capsys)
    # The paths are under tmp_path and name the run, and the fix commit is dated now.
    document["repo_path"] = "<repo>"
    document["patch_path"] = "<out-dir>/<pipeline-run-id>.patch"
    document["report_path"] = "<out-dir>/<pipeline-run-id>.json"
    document["panel"]["head"] = "<fix-sha>"
    golden("fix", document)


# --- cost

COST_T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
# A day of work: two pipelines, an eval run, a publish run and a loose, unpriced Jev run.
COST_RUNS: list[tuple[str, str, str | None, float | None, str | None]] = [
    ("r1", "code-reviewer@1.2.0", "pipe-1", 0.012345, None),
    ("r2", "perf-reviewer@0.3.0", "pipe-1", 0.0231, None),
    ("r3", "judge@1.0.0", "pipe-1", 0.004, None),
    ("r4", "code-reviewer@1.2.0", "pipe-2", 0.0119, None),
    ("r5", "code-reviewer@1.2.0", None, 0.3, "eval:code-reviewer@1.2.0"),
    ("r6", "code-reviewer@1.2.0", None, 0.2, "eval:code-reviewer@1.2.0"),
    ("r7", "fixer@0.1.0", None, 0.45, "publish:fixer@0.1.0"),
    ("r8", "diff-triage@0.1.0", None, None, None),
]


def test_cost_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    path = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(path))
    with Trace(path) as trace:
        for minute, (run_id, agent_ref, pipeline, cost, tag) in enumerate(COST_RUNS, start=1):
            trace.record(
                TraceRow(
                    run_id=run_id,
                    pipeline_run_id=pipeline,
                    agent_ref=agent_ref,
                    kind="jev" if agent_ref.startswith("diff-triage@") else "llm",
                    started_at=COST_T0 + timedelta(minutes=minute),
                    duration_ms=900,
                    input_json={},
                    cost_usd=cost,
                    tag=tag,
                )
            )
    golden("cost", _run(["cost", "--since", "2026-10-20", "--json"], capsys))
    golden(
        "cost-by-kind", _run(["cost", "--since", "2026-10-20", "--by", "kind", "--json"], capsys)
    )
