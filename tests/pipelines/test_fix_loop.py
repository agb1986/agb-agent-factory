"""Tests for pipelines/fix_loop.py: the fix loop, its test-file guard and `factory fix`.

No Docker and no API key. The test-writer and the fixer are fakes that change files in the clone
the way the real agents do through `fs.write_file`, the fixer's writes going through the hook it is
given first, and the sandbox is a fake that reports what it was asked to run.
"""

import json
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.cli import main
from factory.exit import ExitCode, UsageError
from fixtures.build import build
from pipelines import fix_loop
from pipelines.fix_loop import (
    FIXER,
    TEST_WRITE_REFUSED_TAG,
    TEST_WRITER,
    FixAgents,
    FixError,
    FixResult,
    SuiteFailedError,
    TestFileGuard,
    TestFileWriteError,
    load_finding,
    resolve_fix_agents,
    run_fix,
)
from pipelines.sandbox import DirtyRepositoryError, checkout, clean
from registry.spec import LLMSpec
from runtime.budget import Budget, BudgetExceeded
from runtime.hooks import ToolCall
from runtime.trace import Trace, TraceRow
from tests.pipelines.test_review_panel import (
    BLOCKING,
    RUN_ID,
    FakeJev,
    FakeRunner,
    _judgement,
)
from tools._common import ToolExecutionError, resolve_commit
from tools.sandbox import SandboxResult

FINDING: dict[str, Any] = {
    "file": "series/rolling.py",
    "line_start": 12,
    "line_end": 13,
    "severity": "major",
    "category": "logic",
    "title": "Loop bound drops the last window",
    "body": "`range(window, len(values) - 1)` stops one step early, so the last window is lost.",
}
TEST_PATH = "tests/test_last_window.py"
TEST_SOURCE = """from series.rolling import rolling_sums


def test_every_window_is_summed() -> None:
    assert rolling_sums([1, 2, 3, 4], 2) == [3, 5, 7]
"""
SOURCE = "series/rolling.py"
PASSED = b"6 passed in 0.02s\n"


def _write(clone: Path, path: str, content: str) -> None:
    target = clone / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _fixed(clone: Path) -> str:
    return (clone / SOURCE).read_text(encoding="utf-8").replace("len(values) - 1", "len(values)")


class FixRunner(FakeRunner):
    """`FakeRunner` with a test-writer and a fixer that change files in the clone.

    The test-writer writes `tests` (path to content) and reports `claimed` as changed and as a test
    file too, without writing it. The fixer asks its hook about each of
    `fixes` (default: the real fix to `series/rolling.py`) and writes it when allowed, then writes
    `unhooked` without asking, as a script run through `shell.run` would. Both trace a row and
    charge `0.25` to the budget, as the real runner does after a run.
    """

    def __init__(
        self,
        judgement: Mapping[str, Any] | None = None,
        *,
        tests: Mapping[str, str] | None = None,
        fixes: Mapping[str, str] | None = None,
        unhooked: Mapping[str, str] | None = None,
        claimed: Sequence[str] = (),
    ) -> None:
        super().__init__(judgement)
        self.tests = {TEST_PATH: TEST_SOURCE} if tests is None else dict(tests)
        self.claimed = list(claimed)
        self.fixes = None if fixes is None else dict(fixes)
        self.unhooked = dict(unhooked or {})

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any] | BaseModel, trace: Trace, **kwargs: Any
    ) -> BaseModel:
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        if spec.name not in (TEST_WRITER[0], FIXER[0]):
            return super().__call__(spec, data, trace, **kwargs)
        self.calls.append({"spec": spec, "payload": data, **kwargs})
        clone = Path(kwargs["repo_path"])
        if spec.name == TEST_WRITER[0]:
            for path, content in self.tests.items():
                _write(clone, path, content)
            output: dict[str, Any] = {
                "files_changed": [
                    {"path": path, "action": "created", "summary": "A test."}
                    for path in [*self.tests, *self.claimed]
                ],
                "commands_run": [],
                "test_files": [*self.tests, *self.claimed],
                "fails_on_head": True,
                "summary": "The last window is missing.",
            }
        else:
            fixes = {SOURCE: _fixed(clone)} if self.fixes is None else self.fixes
            for path, content in fixes.items():
                call = ToolCall("fs.write_file", {"path": path, "content": content}, spec.address)
                kwargs["hook"].before_tool(call)
                _write(clone, path, content)
            for path, content in self.unhooked.items():
                _write(clone, path, content)
            output = {
                "files_changed": [
                    {"path": path, "action": "modified", "summary": "A fix."}
                    for path in [*fixes, *self.unhooked]
                ],
                "commands_run": [],
                "nothing_to_fix": not (fixes or self.unhooked),
                "suite_passes": True,
                "summary": "The loop now runs to len(values).",
            }
        trace.record(
            TraceRow(
                run_id=f"row-{spec.name}",
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=data,
                cost_usd=0.25,
            )
        )
        kwargs["budget"].charge(0.25)
        return spec.output_class.model_validate(output)

    def names(self) -> list[str]:
        return [call["spec"].name for call in self.calls]


class FakeSandbox:
    """Stands in for `DockerSandbox`: records what it is asked and answers `exit_code`."""

    def __init__(self, exit_code: int = 0, output: bytes = PASSED) -> None:
        self.exit_code = exit_code
        self.output = output
        self.checked = 0
        self.runs: list[tuple[Path, tuple[str, ...], bool]] = []
        self.cleaned: list[tuple[Path, bool]] = []

    def check(self) -> None:
        self.checked += 1

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        self.runs.append((root, tuple(argv), root.is_dir()))
        return SandboxResult(tuple(argv), self.exit_code, self.output, 0.1)

    def cleanup(self, root: Path) -> None:
        self.cleaned.append((root, root.is_dir()))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    (fixture,) = build(["off-by-one-red"], dest=tmp_path_factory.mktemp("repos"))
    return fixture.path


@pytest.fixture(scope="module")
def agents() -> FixAgents:
    return resolve_fix_agents(allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


@pytest.fixture
def finding() -> Any:
    return load_finding(json.dumps(FINDING))


def _fix(
    agents: FixAgents,
    repo: Path,
    trace: Trace,
    tmp_path: Path,
    *,
    runner: FixRunner | None = None,
    sandbox: FakeSandbox | None = None,
    **kwargs: Any,
) -> FixResult:
    kwargs.setdefault("jev_runner", FakeJev())
    kwargs.setdefault("work_dir", _work_dir(tmp_path))
    return run_fix(
        agents,
        repo,
        load_finding(json.dumps(FINDING)),
        trace,
        out_dir=tmp_path / "out",
        sandbox=sandbox or FakeSandbox(),
        runner=runner or FixRunner(),
        pipeline_run_id=RUN_ID,
        **kwargs,
    )


def _work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def _refusals(trace: Trace) -> list[tuple[Any, ...]]:
    trace.ensure_schema()
    return (
        trace._connect()
        .execute(
            "SELECT agent_ref, pipeline_run_id, cost_usd, decision_json, error FROM traces "
            "WHERE tag = ?",
            [TEST_WRITE_REFUSED_TAG],
        )
        .fetchall()
    )


# --- resolving the agents


def test_the_agents_resolve_to_sandboxed_workers_and_the_full_panel(agents: FixAgents) -> None:
    assert agents.test_writer.address == "test-writer@0.1.0"
    assert agents.fixer.address == "fixer@0.1.0"
    assert agents.test_writer.sandbox and agents.fixer.sandbox
    assert agents.fixer.guard is not None
    assert agents.panel.triage is not None and agents.panel.gate is not None


def test_a_fixer_without_a_guard_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = fix_loop.resolve

    def unguarded(name: str, *args: Any, **kwargs: Any) -> Any:
        spec = real(name, *args, **kwargs)
        return spec.model_copy(update={"guard": None}) if name == FIXER[0] else spec

    monkeypatch.setattr(fix_loop, "resolve", unguarded)
    with pytest.raises(UsageError, match="no action guard"):
        resolve_fix_agents(allow_unpublished=True)


def test_an_unsandboxed_worker_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = fix_loop.resolve

    def unsandboxed(name: str, *args: Any, **kwargs: Any) -> Any:
        spec = real(name, *args, **kwargs)
        return spec.model_copy(update={"sandbox": False}) if name == TEST_WRITER[0] else spec

    monkeypatch.setattr(fix_loop, "resolve", unsandboxed)
    with pytest.raises(UsageError, match="not sandboxed"):
        resolve_fix_agents(allow_unpublished=True)


# --- loading the finding


def test_a_finding_loads_from_json(finding: Any) -> None:
    assert finding.file == "series/rolling.py"
    assert finding.line_start == 12


def test_a_finding_loads_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "finding.json"
    path.write_text(json.dumps(FINDING), encoding="utf-8")
    assert load_finding(f"@{path}").title == FINDING["title"]


def test_a_panel_document_gives_the_judges_finding_without_sources(tmp_path: Path) -> None:
    second = FINDING | {"title": "Second", "sources": ["code-reviewer@1.2.0"]}
    judgement = _judgement(BLOCKING)
    judgement["merged_findings"].append(second)
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"judge": {"output": judgement}}), encoding="utf-8")
    assert load_finding(f"@{path}").file == "shop/shipping.py"
    assert load_finding(f"@{path}", index=1).title == "Second"


def test_a_finding_index_out_of_range_is_a_usage_error() -> None:
    document = {"judge": {"output": _judgement(BLOCKING)}}
    with pytest.raises(UsageError, match="out of range: the judge merged 1 findings"):
        load_finding(json.dumps(document), index=1)


def test_a_panel_document_without_a_judgement_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="no judgement"):
        load_finding(json.dumps({"judge": None}))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "not valid JSON"),
        (json.dumps(FINDING | {"severity": "huge"}), "--finding is not a finding: severity"),
        ("@/nonexistent/finding.json", "cannot read the finding"),
    ],
    ids=["bad-json", "bad-severity", "missing-file"],
)
def test_a_bad_finding_is_a_usage_error(text: str, message: str) -> None:
    with pytest.raises(UsageError, match=message):
        load_finding(text)


# --- the test-file guard


@pytest.mark.parametrize(
    "path", ["tests/test_x.py", "./tests/test_x.py", "series/../tests/test_x.py", "conftest.py"]
)
def test_the_guard_refuses_a_write_to_the_test_layout(
    path: str, tmp_path: Path, trace: Trace
) -> None:
    guard = TestFileGuard(tmp_path.resolve(), frozenset(), trace, RUN_ID)
    call = ToolCall("fs.write_file", {"path": path, "content": ""}, "fixer@0.1.0")
    with pytest.raises(TestFileWriteError, match="in the test layout"):
        guard.before_tool(call)
    ((agent, run_id, cost, decision, error),) = _refusals(trace)
    assert (agent, run_id, cost) == ("fixer@0.1.0", RUN_ID, 0.0)
    assert json.loads(decision) == {
        "allow": False,
        "path": os.path.normpath(path),
        "reason": "the path is in the test layout",
        "check": "hook",
    }
    assert "may not change the test file" in error


def test_the_guard_refuses_an_absolute_path_and_a_symlink_into_tests(
    tmp_path: Path, trace: Trace
) -> None:
    clone = tmp_path.resolve()
    (clone / "tests").mkdir()
    (clone / "alias").symlink_to(clone / "tests")
    guard = TestFileGuard(clone, frozenset(), trace, RUN_ID)
    for path in (str(clone / "tests" / "test_x.py"), "alias/helpers.py"):
        with pytest.raises(TestFileWriteError):
            guard.before_tool(ToolCall("fs.write_file", {"path": path}, "fixer@0.1.0"))
    paths = [json.loads(row[3])["path"] for row in _refusals(trace)]
    assert paths == ["tests/test_x.py", "tests/helpers.py"]


def test_the_guard_refuses_a_path_the_test_writer_changed(tmp_path: Path, trace: Trace) -> None:
    guard = TestFileGuard(tmp_path.resolve(), frozenset({"data/cases.json"}), trace, RUN_ID)
    with pytest.raises(TestFileWriteError, match="the test-writer changed this path"):
        guard.before_tool(ToolCall("fs.write_file", {"path": "data/cases.json"}, "fixer@0.1.0"))


CONFIGURES = "the file configures the test run"
SHADOWS = "the path shadows the test runner"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("pytest.ini", CONFIGURES),
        ("./pyproject.toml", CONFIGURES),
        ("setup.cfg", CONFIGURES),
        ("tox.ini", CONFIGURES),
        ("pytest.py", SHADOWS),
        ("pluggy.cpython-312.pyc", SHADOWS),
        ("pytest/__init__.py", SHADOWS),
        ("_pytest/python.py", SHADOWS),
        ("packaging.py", SHADOWS),
        ("packaging/version.py", SHADOWS),
    ],
)
def test_the_guard_refuses_what_controls_the_test_run(
    path: str, reason: str, tmp_path: Path, trace: Trace
) -> None:
    guard = TestFileGuard(tmp_path.resolve(), frozenset(), trace, RUN_ID)
    with pytest.raises(TestFileWriteError, match=reason):
        guard.before_tool(ToolCall("fs.write_file", {"path": path}, "fixer@0.1.0"))
    ((_, _, _, decision, _),) = _refusals(trace)
    assert json.loads(decision)["reason"] == reason


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("fs.write_file", {"path": "series/rolling.py"}),
        ("fs.write_file", {"path": "series/pyproject.toml"}),
        ("fs.write_file", {"path": "series/pytest/helpers.py"}),
        ("fs.write_file", {"path": "pytest.md"}),
        ("fs.write_file", {"path": "pytest_helpers.py"}),
        ("fs.write_file", {"path": "py.typed"}),
        ("fs.write_file", {"path": "py/__init__.py"}),
        ("fs.write_file", {"path": "pygments/lexer.py"}),
        ("fs.write_file", {"path": "../outside/tests/test_x.py"}),
        ("fs.write_file", {"content": "no path"}),
        ("shell.run", {"argv": ["python", "-m", "pytest", "-q"]}),
        ("git.read_file", {"path": "tests/test_rolling.py"}),
    ],
)
def test_the_guard_allows_everything_else(
    name: str, arguments: dict[str, Any], tmp_path: Path, trace: Trace
) -> None:
    guard = TestFileGuard(tmp_path.resolve(), frozenset(), trace, RUN_ID)
    assert guard.before_tool(ToolCall(name, arguments, "fixer@0.1.0")).allowed
    assert _refusals(trace) == []


# --- running the loop


def test_a_fix_the_panel_approves_writes_a_patch_and_a_report(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    before = _git(repo, "rev-parse", "HEAD"), _git(repo, "for-each-ref")
    runner, sandbox = FixRunner(), FakeSandbox()
    result = _fix(agents, repo, trace, tmp_path, runner=runner, sandbox=sandbox)

    assert result.ship
    assert runner.names()[:2] == ["test-writer", "fixer"]
    assert {call["pipeline_run_id"] for call in runner.calls} == {RUN_ID}
    assert result.files_changed == (
        (SOURCE, "modified", ("fixer",)),
        (TEST_PATH, "created", ("test-writer",)),
    )
    assert result.head == before[0].strip()
    assert result.repo_path == str(repo.resolve())
    assert result.total_cost_usd == trace.cost_by_pipeline_run(RUN_ID)

    assert result.patch_path == tmp_path / "out" / f"{RUN_ID}.patch"
    subprocess.run(["git", "apply", "--check", str(result.patch_path)], cwd=repo, check=True)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report == json.loads(json.dumps(result.to_json()))
    assert report["suite"] == {
        "argv": ["python", "-m", "pytest", "-q"],
        "exit_code": 0,
        "passed": True,
        "seconds": 0.1,
        "output_tail": PASSED.decode(),
    }
    assert report["panel"]["pipeline_run_id"] == RUN_ID

    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "for-each-ref")) == before
    assert _git(repo, "status", "--porcelain") == ""
    assert list(_work_dir(tmp_path).iterdir()) == []


def test_the_suite_and_the_panel_see_the_fixed_clone(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    runner, sandbox = FixRunner(), FakeSandbox()
    result = _fix(agents, repo, trace, tmp_path, runner=runner, sandbox=sandbox)
    ((root, argv, existed),) = sandbox.runs
    assert argv == ("python", "-m", "pytest", "-q") and existed
    assert sandbox.checked == 1
    assert sandbox.cleaned == [(root, True)]
    reviews = [call for call in runner.calls if call["spec"].name.endswith("-reviewer")]
    assert reviews and {str(call["repo_path"]) for call in reviews} == {str(root)}
    assert result.panel.base == result.head
    assert result.panel.head != result.head


def test_a_blocking_judgement_writes_the_report_and_no_patch(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    result = _fix(agents, repo, trace, tmp_path, runner=FixRunner(_judgement(BLOCKING)))
    assert not result.ship
    assert result.patch_path is None
    assert json.loads(result.report_path.read_text(encoding="utf-8"))["ship"] is False
    assert [path.name for path in (tmp_path / "out").iterdir()] == [f"{RUN_ID}.json"]
    assert "no patch" in result.text()


def test_a_failing_suite_ends_the_run_before_the_panel(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    runner = FixRunner()
    sandbox = FakeSandbox(exit_code=1, output=b"E   assert [3, 5] == [3, 5, 7]\n1 failed\n")
    with pytest.raises(SuiteFailedError, match="exit code 1") as raised:
        _fix(agents, repo, trace, tmp_path, runner=runner, sandbox=sandbox)
    assert raised.value.hint is not None and "1 failed" in raised.value.hint
    assert runner.names() == ["test-writer", "fixer"]
    assert list((tmp_path / "out").iterdir()) == []
    assert len(sandbox.cleaned) == 1
    assert list(_work_dir(tmp_path).iterdir()) == []


def test_the_fixer_writing_a_test_through_its_tools_is_refused(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    runner = FixRunner(fixes={TEST_PATH: "def test_every_window_is_summed(): pass\n"})
    with pytest.raises(TestFileWriteError, match=f"test file {TEST_PATH}"):
        _fix(agents, repo, trace, tmp_path, runner=runner)
    ((agent, run_id, _, decision, _),) = _refusals(trace)
    assert (agent, run_id) == ("fixer@0.1.0", RUN_ID)
    assert json.loads(decision)["check"] == "hook"
    assert json.loads(decision)["reason"] == "the test-writer changed this path"
    assert list(_work_dir(tmp_path).iterdir()) == []


def test_a_test_changed_by_another_route_is_refused_after_the_fixer(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    runner = FixRunner(unhooked={"tests/test_rolling.py": "def test_nothing(): pass\n"})
    sandbox = FakeSandbox()
    with pytest.raises(TestFileWriteError, match="tests/test_rolling.py"):
        _fix(agents, repo, trace, tmp_path, runner=runner, sandbox=sandbox)
    ((agent, _, _, decision, _),) = _refusals(trace)
    assert agent == "fixer@0.1.0"
    assert json.loads(decision) == {
        "allow": False,
        "path": "tests/test_rolling.py",
        "reason": "the path is in the test layout",
        "check": "post-check",
    }
    assert sandbox.runs == []


def test_a_file_the_test_writer_wrote_outside_tests_is_protected(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    tests = {TEST_PATH: TEST_SOURCE, "series/cases.json": "[]\n"}
    runner = FixRunner(tests=tests, unhooked={"series/cases.json": "[1]\n"})
    with pytest.raises(TestFileWriteError, match="the test-writer changed this path"):
        _fix(agents, repo, trace, tmp_path, runner=runner)


def test_what_the_test_writer_only_reports_is_not_protected(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    result = _fix(agents, repo, trace, tmp_path, runner=FixRunner(claimed=[SOURCE]))
    assert result.ship
    assert (SOURCE, "modified", ("fixer",)) in result.files_changed
    assert _refusals(trace) == []


@pytest.mark.parametrize(
    ("path", "content", "reason"),
    [
        ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-k nothing'\n", CONFIGURES),
        ("pytest.ini", "[pytest]\n", CONFIGURES),
        ("pytest.py", "raise SystemExit(0)\n", SHADOWS),
        ("packaging.py", "raise SystemExit(0)\n", SHADOWS),
    ],
)
def test_a_change_to_the_test_run_by_another_route_is_refused_after_the_fixer(
    path: str,
    content: str,
    reason: str,
    agents: FixAgents,
    repo: Path,
    trace: Trace,
    tmp_path: Path,
) -> None:
    sandbox = FakeSandbox()
    with pytest.raises(TestFileWriteError, match=reason):
        _fix(
            agents,
            repo,
            trace,
            tmp_path,
            runner=FixRunner(unhooked={path: content}),
            sandbox=sandbox,
        )
    ((_, _, _, decision, _),) = _refusals(trace)
    assert json.loads(decision) == {
        "allow": False,
        "path": path,
        "reason": reason,
        "check": "post-check",
    }
    assert sandbox.runs == []


def test_the_suite_runs_over_the_commits_alone(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    left = ["series/__pycache__/rolling.cpython-312.pyc", "local/conf.py"]
    seen: dict[str, bool] = {}

    class LookingSandbox(FakeSandbox):
        def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
            seen.update({path: (root / path).exists() for path in [*left, ".gitignore"]})
            seen["fixed"] = "len(values) - 1" not in (root / SOURCE).read_text(encoding="utf-8")
            return super().run(root, argv)

    runner = FixRunner(unhooked={".gitignore": "local/\n", **dict.fromkeys(left, "")})
    result = _fix(agents, repo, trace, tmp_path, runner=runner, sandbox=LookingSandbox())
    assert seen == {left[0]: False, left[1]: False, ".gitignore": True, "fixed": True}
    assert result.ship


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_clean_fails_on_what_it_cannot_delete_and_the_clone_still_goes(
    repo: Path, tmp_path: Path
) -> None:
    work = _work_dir(tmp_path)
    with checkout(repo, resolve_commit(repo, "HEAD"), work_dir=work) as clone:
        (clone / "locked").mkdir()
        (clone / "locked" / "left.txt").write_text("", encoding="utf-8")
        (clone / "locked").chmod(0o500)
        with pytest.raises(ToolExecutionError, match="could not be cleaned: locked/left.txt"):
            clean(clone)
    assert list(work.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_clone_with_unreadable_directories_is_deleted(repo: Path, tmp_path: Path) -> None:
    work = _work_dir(tmp_path)
    with checkout(repo, resolve_commit(repo, "HEAD"), work_dir=work) as clone:
        deeper = clone / "locked" / "deeper"
        deeper.mkdir(parents=True)
        (deeper / "left.txt").write_text("", encoding="utf-8")
        deeper.chmod(0o500)
        (clone / "locked").chmod(0o000)
    assert list(work.iterdir()) == []


def test_a_run_that_changes_nothing_is_an_error(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    sandbox = FakeSandbox()
    with pytest.raises(FixError, match="changed nothing"):
        _fix(agents, repo, trace, tmp_path, runner=FixRunner(tests={}, fixes={}), sandbox=sandbox)
    assert sandbox.runs == []


def test_a_run_past_its_budget_stops(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    runner, sandbox = FixRunner(), FakeSandbox()
    with pytest.raises(BudgetExceeded):
        _fix(agents, repo, trace, tmp_path, runner=runner, sandbox=sandbox, budget=Budget(0.3))
    assert runner.names() == ["test-writer", "fixer"]
    assert sandbox.runs == [] and len(sandbox.cleaned) == 1
    assert list(_work_dir(tmp_path).iterdir()) == []


def test_the_sandbox_is_checked_before_any_agent_runs(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    class Missing(FakeSandbox):
        def check(self) -> None:
            raise UsageError("no docker")

    runner = FixRunner()
    with pytest.raises(UsageError, match="no docker"):
        _fix(agents, repo, trace, tmp_path, runner=runner, sandbox=Missing())
    assert runner.calls == []


def test_a_dirty_repository_is_refused(agents: FixAgents, tmp_path: Path, trace: Trace) -> None:
    (fixture,) = build(["off-by-one-red"], dest=tmp_path / "repos")
    (fixture.path / "notes.txt").write_text("draft\n", encoding="utf-8")
    runner = FixRunner()
    with pytest.raises(DirtyRepositoryError, match="notes.txt"):
        _fix(agents, fixture.path, trace, tmp_path, runner=runner)
    assert runner.calls == []


def test_a_work_dir_that_is_not_a_directory_is_a_usage_error(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    with pytest.raises(UsageError, match="is not a directory"):
        _fix(agents, repo, trace, tmp_path, work_dir=tmp_path / "missing")


def test_a_work_dir_with_a_colon_is_a_usage_error(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    work = tmp_path / "a:b"
    work.mkdir()
    runner = FixRunner()
    with pytest.raises(UsageError, match="has a ':' in its path"):
        _fix(agents, repo, trace, tmp_path, runner=runner, work_dir=work)
    assert runner.calls == []


def test_head_picks_the_commit_to_start_from(
    agents: FixAgents, repo: Path, trace: Trace, tmp_path: Path
) -> None:
    base = _git(repo, "rev-parse", "base").strip()
    result = _fix(agents, repo, trace, tmp_path, head="base")
    assert result.head == base


# --- factory fix


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FixRunner:
    """Point `factory fix` at fakes: the runners, the sandbox, the trace database and the key."""
    runner = FixRunner()
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(fix_loop, "run", runner)
    monkeypatch.setattr(fix_loop, "run_jev", FakeJev())
    monkeypatch.setattr(fix_loop, "DockerSandbox", FakeSandbox)
    return runner


def _argv(repo: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "fix",
        str(repo),
        "--finding",
        json.dumps(FINDING),
        "--out-dir",
        str(tmp_path / "out"),
        "--work-dir",
        str(_work_dir(tmp_path)),
        "--allow-unpublished",
        *extra,
    ]


def test_factory_fix_prints_the_report_and_exits_0(
    cli: FixRunner, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(repo, tmp_path, "--json")) == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ship"] is True
    assert Path(document["patch_path"]).is_file()
    assert "fix run" in captured.err


def test_factory_fix_exits_1_when_the_panel_does_not_approve(
    cli: FixRunner, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.judgement = _judgement(BLOCKING)
    assert main(_argv(repo, tmp_path)) == ExitCode.ERROR
    assert "no patch" in capsys.readouterr().out


def test_factory_fix_needs_the_api_key(
    cli: FixRunner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert main(_argv(repo, tmp_path)) == ExitCode.AUTH_OR_CONFIG
    assert cli.calls == []


def test_factory_fix_with_a_bad_finding_is_a_usage_error(
    cli: FixRunner, repo: Path, tmp_path: Path
) -> None:
    argv = _argv(repo, tmp_path)
    argv[3] = "{not json"
    assert main(argv) == ExitCode.USAGE
