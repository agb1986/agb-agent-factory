"""Golden tests for the `--json` documents of the `factory` commands.

Each test runs a command through `factory.cli.main` with fakes in place of the model, parses the one
document on stdout, blanks the fields that change per run and compares it to `tests/golden/<name>.json`.
Refresh the files with `pytest tests/test_golden_cli.py --update-golden`; see `tests/golden/README.md`.
"""

import json
import subprocess
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
from pipelines import build_loop, fix_loop, hotfix, incident, plan_loop, review_panel, triage
from registry import AgentRef
from registry.common_schemas import Ticket
from registry.spec import LLMSpec
from runtime import run_once
from runtime.escalations import AgentOutput, escalate
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItems
from tests.gh_fake import FakeGh, github_repo
from tests.pipelines.test_build_loop import FULL, PULL_JSON, REPO_JSON, BuildRunner
from tests.pipelines.test_build_loop import _ticket as build_ticket
from tests.pipelines.test_fix_loop import FINDING, FakeSandbox, FixRunner
from tests.pipelines.test_hotfix import SequencedSandbox, hotfix_repo, hotfix_ticket, route_gh
from tests.pipelines.test_hotfix import _runner as hotfix_runner
from tests.pipelines.test_incident import FakeSandbox as IncidentSandbox
from tests.pipelines.test_incident import IncidentRunner
from tests.pipelines.test_incident import _event as incident_event
from tests.pipelines.test_plan_loop import QUESTIONS, SHARED, UNKNOWN
from tests.pipelines.test_plan_loop import FakeRunner as PlanRunner
from tests.pipelines.test_plan_loop import _prd as plan_prd
from tests.pipelines.test_plan_loop import _story as plan_story
from tests.pipelines.test_review_panel import FakeJev
from tests.test_run_cli import PAYLOAD, FakeRunner, write_llm

Golden = Callable[[str, Any], None]

# Fields that differ on every run: the value they are replaced with before comparing.
VOLATILE: dict[str, Any] = {
    "duration_ms": 0,
    "git_sha": "<git-sha>",
    "pipeline_run_id": "pipeline-run-1",
    "eval_run_id": "eval-run-1",
    "run_id": "run-1",
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


# --- plan


def test_plan_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    story = dict(SHARED)
    story["tickets"] = [*SHARED["tickets"], *UNKNOWN["tickets"][1:]]
    story["open_questions"] = ["Which database?"]
    monkeypatch.setattr(
        plan_loop,
        "run",
        PlanRunner(
            plan_prd(QUESTIONS),
            plan_story(*story["tickets"], open_questions=story["open_questions"]),
        ),
    )
    (fixture,) = build(["off-by-one-red"], dest=tmp_path / "repos")
    plans = tmp_path / "plans"
    argv = ["plan", str(fixture.path), "--plans-dir", str(plans), "--allow-unpublished", "--json"]
    first = _run([*argv, "--idea", "Quote shipping by weight"], capsys)
    answers = ["--answer", "1=euros", "--answer", "2=yes"]
    document = _run([*argv, "--prd", f"@{first['prd_path']}", "--approve", *answers], capsys)
    # The paths are under tmp_path and the commit is the fixture's.
    document["prd_path"] = "<plans-dir>/<pipeline-run-id>.md"
    document["repository"] = "<repo>"
    document["ref"] = "<sha>"
    golden("plan", document)


# --- incident


def test_incident_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(incident, "run", IncidentRunner())
    monkeypatch.setattr(incident, "DockerSandbox", IncidentSandbox)
    (fixture,) = build(["crash-empty-average"], dest=tmp_path / "repos")
    out, work = tmp_path / "out", tmp_path / "work"
    work.mkdir()
    event = json.dumps(incident_event())
    argv = ["incident", str(fixture.path), "--event", event, "--out-dir", str(out)]
    document = _run([*argv, "--work-dir", str(work), "--allow-unpublished", "--json"], capsys)
    # The paths are under tmp_path and name the run.
    document["repo_path"] = document["ticket"]["repo"] = "<repo>"
    document["report_path"] = "<out-dir>/<pipeline-run-id>.json"
    golden("incident", document)


# --- build


def test_build_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(build_loop, "run", BuildRunner())
    monkeypatch.setattr(build_loop, "run_jev", FakeJev())
    monkeypatch.setattr(build_loop, "DockerSandbox", FakeSandbox)
    gh = FakeGh(tmp_path, monkeypatch)
    gh.route("GET", f"repos/{FULL}", REPO_JSON)
    gh.route("POST", f"repos/{FULL}/pulls", PULL_JSON)
    repo = github_repo(tmp_path)
    with Trace(tmp_path / "traces.duckdb") as trace:
        items = WorkItems(trace)
        item = items.add(build_ticket())
        items.ready()
    out, work = tmp_path / "out", tmp_path / "work"
    work.mkdir()
    argv = ["build", item.id, "--repo", str(repo.root), "--out-dir", str(out)]
    assert main([*argv, "--work-dir", str(work), "--allow-unpublished", "--json"]) == ExitCode.OK
    raw = json.loads(capsys.readouterr().out)
    # The run id is in the branch and the body, the paths are under tmp_path and the commits are
    # dated now.
    text = json.dumps(raw).replace(raw["pipeline_run_id"], "pipeline-run-1")
    for round_ in raw["rounds"]:
        text = text.replace(round_["commit"], "<commit>")
    text = text.replace(raw["head"], "<head>").replace(str(tmp_path), "<tmp>")
    golden("build", _normalised(json.loads(text)))


def test_hotfix_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(hotfix, "run", hotfix_runner())
    monkeypatch.setattr(hotfix, "run_jev", FakeJev())
    monkeypatch.setattr(hotfix, "DockerSandbox", SequencedSandbox)
    route_gh(FakeGh(tmp_path, monkeypatch))
    repo = hotfix_repo(tmp_path)
    with Trace(tmp_path / "traces.duckdb") as trace:
        items = WorkItems(trace)
        item = items.add(hotfix_ticket())
        items.ready()
    out, work = tmp_path / "out", tmp_path / "work"
    work.mkdir()
    argv = ["hotfix", item.id, "--repo", str(repo.root), "--out-dir", str(out)]
    assert main([*argv, "--work-dir", str(work), "--allow-unpublished", "--json"]) == ExitCode.OK
    raw = json.loads(capsys.readouterr().out)
    # The run id is in the branch and the body, the paths are under tmp_path, the commits are dated
    # now and the pull request opens now, so the time from the event to it grows by the second.
    raw["timing"] |= {"pr_opened_at": "<now>", "time_to_pr_seconds": 0}
    text = json.dumps(raw).replace(raw["pipeline_run_id"], "pipeline-run-1")
    for commit in (raw["reproduction"]["commit"], raw["fix"]["commit"]):
        text = text.replace(commit, "<commit>")
    text = text.replace(raw["head"], "<head>").replace(str(tmp_path), "<tmp>")
    golden("hotfix", _normalised(json.loads(text)))


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


# --- ticket


def test_ticket_show_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    path = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(path))
    clock = iter(COST_T0 + timedelta(minutes=minute) for minute in range(1, 100))
    ticket = Ticket(
        kind="feature",
        title="Add the work-item store",
        body="Keep work items in the trace store.",
        acceptance=["factory ticket show prints the item", "an illegal move exits 2"],
        repo="agb1986/agb-agent-factory",
        ref="main",
        impacted_paths=["runtime/work_items.py"],
    )
    with Trace(path) as trace:
        store = WorkItems(trace, now=lambda: next(clock))
        store.add(ticket.model_copy(update={"kind": "docs", "title": "Write the ADR"}))
        store.add(ticket, parent="wi-1", depends_on=["wi-1"], size="M")
        for to in ("ready", "in_progress", "in_review", "done"):
            store.move("wi-1", to, f"to {to}")
        store.ready()
        store.move("wi-2", "in_progress", "picked", actor="pipeline:feature_loop",
                   pipeline_run_id="pipe-1")  # fmt: skip
        store.link_run("wi-2", "pipe-1")
        for run_id, agent_ref, pipeline, cost, tag in COST_RUNS[:3]:
            trace.record(
                TraceRow(
                    run_id=run_id, pipeline_run_id=pipeline, agent_ref=agent_ref, kind="llm",
                    started_at=COST_T0, duration_ms=900, input_json={}, cost_usd=cost, tag=tag,
                )
            )  # fmt: skip
    golden("ticket-show", _run(["ticket", "show", "wi-2", "--json"], capsys))


# --- escalations


def test_escalation_show_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    golden: Golden,
) -> None:
    path = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(path))
    clock = iter(COST_T0 + timedelta(minutes=minute) for minute in range(1, 100))
    ticket = build_ticket()
    with Trace(path) as trace:
        store = WorkItems(trace, now=lambda: next(clock))
        item_id = store.add(ticket).id
        store.move(item_id, "ready", "approved")
        store.move(item_id, "in_progress", "picked", actor=build_loop.ACTOR,
                   pipeline_run_id="pipe-1")  # fmt: skip
        store.link_run(item_id, "pipe-1")
        trace.record(
            TraceRow(
                run_id="run-a", pipeline_run_id="pipe-1", agent_ref="feature-coder@0.1.0",
                kind="llm", started_at=COST_T0, duration_ms=900, input_json={}, cost_usd=0.25,
            )
        )  # fmt: skip
        escalate(
            trace,
            item_id,
            trigger="blocked_output",
            question="The coder cannot build the ticket as written: Which module?.\n\n"
            "What should it do?",
            reason="CoderBlockedError: the coder is blocked on wi-1",
            actor=build_loop.ACTOR,
            pipeline_run_id="pipe-1",
            diff="--- a/widgets.py\n+++ b/widgets.py\n@@ -1 +1 @@\n-LIMIT = 5\n+LIMIT = 10\n",
            agent_outputs=[
                AgentOutput("feature-coder@0.1.0", "run-a", '{"blocked": true}'),
                AgentOutput("review panel", None, '{"ship": false}'),
            ],
            suite_tail="1 failed in 0.02s\n",
            now=lambda: next(clock),
        )
    golden("escalation-show", _run(["escalations", "show", "esc-1", "--json"], capsys))


# --- deps

DEPS_FILES = {
    "pkg/__init__.py": "from pkg.a import f\n",
    "pkg/a.py": "import json\n\nfrom . import b\n\n\ndef f(): ...\n",
    "pkg/b.py": "import importlib\n\nimport pkg.a\n\nimportlib.import_module(name)\n",
    "web/main.ts": "import { f } from './lib';\nimport x from '@/alias';\n",
    "web/lib/index.ts": "export const f = 1;\n",
}


def test_deps_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], golden: Golden) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, text in DEPS_FILES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    golden("deps", _run(["deps", str(tmp_path), "--impact", "pkg/b.py", "--json"], capsys))


# --- run


def test_run_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                  capsys: pytest.CaptureFixture[str], golden: Golden) -> None:  # fmt: skip
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(run_once, "run", FakeRunner())
    write_llm(tmp_path, "answerer", "0.1.0")
    (tmp_path / "payload.json").write_text(json.dumps(PAYLOAD))
    argv = ["run", "answerer@^0.1", "--input", "payload.json", "--allow-unpublished", "--json"]
    golden("run", _run(argv, capsys))
