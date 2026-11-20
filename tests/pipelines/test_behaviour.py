"""Tests for pipelines/behaviour.py: the refactor check, alone and inside the build loop.

No Docker, no API key and no network. The unit tests feed the parsers pytest's output as the probe
prints it. One test runs a real probe over a built fixture with `Local`, which runs the sandbox's
command on the host with this interpreter. The build-loop tests reuse the fakes of
`tests/pipelines/test_build_loop.py`; their sandbox answers each probe with scripted output.
"""

import ast
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from evals.coverage import REPORT_MARKER
from evals.graders.sandbox_tests import PYTEST
from factory.exit import ExitCode
from fixtures.build import build
from pipelines.behaviour import (
    MAX_SCRIPT_CHARS,
    PROBE_PYTEST_ARGS,
    BaselineNotGreenError,
    BehaviourChangedError,
    BehaviourCheck,
    CoverageDroppedError,
    CoverageUnmeasuredError,
    Probe,
    TestFileChangedError,
    TestState,
    changed_tests,
    compare,
    parse_states,
    probe,
    probe_script,
    production_files,
    require_green,
    require_no_test_change,
)
from pipelines.build_loop import KIND_CHECKS, BuildAgents, BuildResult, coder_for, run_build
from registry.common_schemas import Ticket
from registry.spec import FilesSpec, LLMSpec
from registry.work_schemas import CoverageReport, FileCoverage
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItems
from tests.gh_fake import FakeGh, GithubRepo, git, github_repo
from tests.pipelines.test_build_loop import (
    FULL,
    PULL_JSON,
    REPO_JSON,
    RUN_ID,
    BuildRunner,
    _branches_tags_and_head,
    _ready,
    _stopped,
    _work_dir,
    _write,
)
from tests.pipelines.test_fix_loop import FakeSandbox
from tests.pipelines.test_review_panel import FakeJev
from tools.sandbox import SandboxResult

HEAD = "0123456789abcdef0123456789abcdef01234567"
TESTS = ("tests/test_app.py::test_one", "tests/test_app.py::test_two")
REFACTORED = {"app.py": "MESSAGE = 'hello'\nprint(MESSAGE)\n"}


def _summary(states: Mapping[str, str], *, header: str = "short test summary info") -> list[str]:
    lines = [f"=========================== {header} ==========================="]
    for test, state in states.items():
        word = state.upper()
        lines.append(f"{word} {test}" if state == "passed" else f"{word} {test} - assert 1 == 2")
    return lines


def _report(missing: Sequence[int] = (), *, statements: int = 4, exit_code: int = 0) -> str:
    file = {
        "path": "app.py",
        "statements": statements,
        "covered": statements - len(missing),
        "missing_lines": list(missing),
    }
    return REPORT_MARKER + json.dumps({"files": [file], "pytest_exit_code": exit_code})


def _output(
    states: Mapping[str, str] | None = None,
    *,
    missing: Sequence[int] = (),
    exit_code: int = 0,
    report: bool = True,
) -> bytes:
    states = {test: "passed" for test in TESTS} if states is None else states
    lines = ["..", *_summary(states), f"{len(states)} tests in 0.02s"]
    if report:
        lines.append(_report(missing, exit_code=exit_code))
    return ("\n".join(lines) + "\n").encode()


def _probe(
    states: Mapping[str, str] | None = None,
    *,
    missing: Sequence[int] = (),
    statements: int = 4,
    exit_code: int = 0,
    measured: bool = True,
) -> Probe:
    states = {test: "passed" for test in TESTS} if states is None else states
    coverage = (
        CoverageReport(
            files=[
                FileCoverage(
                    path="app.py",
                    statements=statements,
                    covered=statements - len(missing),
                    missing_lines=list(missing),
                )
            ],
            pytest_exit_code=exit_code,
        )
        if measured
        else None
    )
    found = {test: _STATE_OF[state] for test, state in states.items()}
    return Probe(exit_code, found, coverage, None if measured else "no report", 0.5)


# --- reading pytest's short summary


def test_parse_states_reads_passed_failed_and_erroring_tests_by_node_id() -> None:
    output = b"\n".join(
        [
            b"..F.E",
            b"=== short test summary info ===",
            b"PASSED tests/test_a.py::test_ok",
            b"PASSED tests/test_a.py::test_ok[a b - c]",
            b"FAILED tests/test_a.py::test_bad - assert 1 == 2",
            b"ERROR tests/test_b.py - ImportError: no module",
            b"ERROR tests/test_a.py::test_fixture - fixture 'x' not found",
            b"SKIPPED [1] tests/test_a.py:9: later",
            b"PASSED ",
            b"1 failed, 2 passed, 2 errors in 0.10s",
        ]
    )
    assert parse_states(output) == {
        "tests/test_a.py::test_ok": "passed",
        "tests/test_a.py::test_ok[a b - c]": "passed",
        "tests/test_a.py::test_bad": "failed",
        "tests/test_b.py": "error",
        "tests/test_a.py::test_fixture": "error",
    }


def test_parse_states_counts_only_lines_after_the_last_summary_header() -> None:
    captured = "\n".join(
        [*_summary({"tests/test_a.py::test_fake": "passed"}), "printed by a test"]
    ).encode()
    output = captured + b"\n" + "\n".join(_summary({"tests/test_a.py::test_x": "failed"})).encode()
    assert parse_states(output) == {"tests/test_a.py::test_x": "failed"}
    assert parse_states(b"PASSED tests/test_a.py::test_x\n") == {}


# --- what a probe runs and over which files


def test_production_files_are_the_tracked_python_files_that_are_not_tests(tmp_path: Path) -> None:
    (fixture,) = build(["refactor-duplicate"], dest=tmp_path / "repos")
    (fixture.path / "report" / "untracked.py").write_text("X = 1\n")
    files = production_files(fixture.path)
    assert "report/lines.py" in files
    assert "report/untracked.py" not in files
    assert not [path for path in files if path.startswith("tests/") or "test_" in path]
    assert files == sorted(files)


def test_probe_script_carries_the_targets_and_pytests_arguments() -> None:
    script = probe_script(["a.py", "b c.py"])
    first, second, rest = script.split("\n", 2)
    assert first == "import json, sys"
    prefix, suffix = "sys.argv = ['-c', *json.loads(", ")]"
    assert second.startswith(prefix) and second.endswith(suffix)
    args = json.loads(ast.literal_eval(second.removeprefix(prefix).removesuffix(suffix)))
    assert args == ["a.py", "b c.py", "--", *PROBE_PYTEST_ARGS]
    assert "@@factory-coverage@@" in rest


class ScriptedSandbox(FakeSandbox):
    """`FakeSandbox` that answers each probe (`python -c`) with the next of `probes`, in order.

    Every other command gets `exit_code` and `output` as before. A probe's exit code is the one its
    coverage report gives, or 0 without one.
    """

    def __init__(self, probes: Sequence[bytes], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.probes = list(probes)

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        if tuple(argv[:2]) != ("python", "-c"):
            return super().run(root, argv)
        self.runs.append((root, tuple(argv), root.is_dir()))
        output = self.probes.pop(0)
        text = output.decode()
        code = (
            json.loads(text.split(REPORT_MARKER)[1].splitlines()[0])["pytest_exit_code"]
            if REPORT_MARKER in text
            else 0
        )
        return SandboxResult(tuple(argv), code, output, 0.3)

    def probe_runs(self) -> list[tuple[str, ...]]:
        return [argv for _, argv, _ in self.runs if argv[:2] == ("python", "-c")]


def _git_repo(tmp_path: Path, files: Mapping[str, str]) -> Path:
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for path, content in files.items():
        _write(root, path, content)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first", "--allow-empty")
    return root


def test_probe_reads_states_coverage_and_exit_code_from_one_sandbox_command(
    tmp_path: Path,
) -> None:
    root = _git_repo(tmp_path, {"app.py": "X = 1\n", "tests/test_app.py": "def test_one(): ...\n"})
    sandbox = ScriptedSandbox([_output(missing=[3], exit_code=1)])
    found = probe(root, sandbox)
    ((argv),) = sandbox.probe_runs()
    assert argv == ("python", "-c", probe_script(["app.py"]))
    assert found.exit_code == 1 and found.passed == list(TESTS)
    assert (found.statements, found.uncovered, found.unmeasured) == (4, 1, None)
    assert found.coverage_text() == "1 uncovered of 4 (75.0%)"
    assert found.to_json() == {
        "exit_code": 1,
        "tests": {"passed": 2, "failed": 0, "error": 0},
        "statements": 4,
        "uncovered": 1,
        "unmeasured": None,
        "seconds": 0.3,
    }


def test_probe_without_a_report_takes_the_commands_exit_code_and_is_unmeasured(
    tmp_path: Path,
) -> None:
    root = _git_repo(tmp_path, {"app.py": "X = 1\n"})
    found = probe(root, FakeSandbox(exit_code=4, output=b"ERROR: usage\n"))
    assert found.exit_code == 4 and found.states == {}
    assert found.coverage is None and found.uncovered is None
    assert found.unmeasured == "the run printed no coverage report"
    assert found.coverage_text() == "unmeasured (the run printed no coverage report)"


def test_probe_with_no_production_file_is_unmeasured_even_with_a_report(tmp_path: Path) -> None:
    root = _git_repo(tmp_path, {"tests/test_app.py": "def test_one(): ...\n"})
    sandbox = ScriptedSandbox([_output()])
    found = probe(root, sandbox)
    assert sandbox.probe_runs() == [("python", "-c", probe_script([]))]
    assert found.coverage is None and found.passed == list(TESTS)
    assert found.unmeasured == "no production .py file is tracked"


def test_probe_with_too_many_files_for_one_argument_measures_nothing(tmp_path: Path) -> None:
    names = {f"pkg/module_with_a_long_name_{n:05}.py": "" for n in range(1000)}
    root = _git_repo(tmp_path, names)
    assert len(probe_script(sorted(names))) > MAX_SCRIPT_CHARS
    sandbox = ScriptedSandbox([_output()])
    found = probe(root, sandbox)
    assert sandbox.probe_runs() == [("python", "-c", probe_script([]))]
    assert found.coverage is None
    assert found.unmeasured == "the 1000 production files do not fit in one sandbox argument"


# --- the rules


def test_require_green_refuses_a_red_or_empty_baseline() -> None:
    require_green(_probe(), HEAD)
    red = _probe({TESTS[0]: "passed", TESTS[1]: "failed"}, exit_code=1)
    with pytest.raises(BaselineNotGreenError) as info:
        require_green(red, HEAD)
    assert str(info.value) == (
        "the suite does not pass at 0123456789ab (exit code 1), so a refactor cannot be shown to "
        f"keep behaviour: {TESTS[1]}"
    )
    with pytest.raises(BaselineNotGreenError, match=r"\(exit code 5\), so .* behaviour$"):
        require_green(_probe({}, exit_code=5), HEAD)
    with pytest.raises(BaselineNotGreenError, match="no test passes at 0123456789ab"):
        require_green(_probe({TESTS[0]: "error"}), HEAD)


def test_require_no_test_change_names_the_test_files() -> None:
    require_no_test_change({"app.py": "modified", "docs/tests.md": "created"})
    changed: dict[str, Any] = {
        "app.py": "modified",
        "tests/data.json": "created",
        "conftest.py": "deleted",
        "pkg/app_test.py": "modified",
    }
    with pytest.raises(TestFileChangedError) as info:
        require_no_test_change(changed)
    assert str(info.value) == (
        "the refactor changed test files: conftest.py, pkg/app_test.py, tests/data.json"
    )


def test_changed_tests_lists_each_test_that_stopped_passing_with_what_it_did() -> None:
    before = _probe({"a": "passed", "b": "passed", "c": "passed", "d": "failed"})
    after = _probe({"a": "passed", "b": "failed", "d": "passed", "e": "error"})
    assert changed_tests(before, after) == [("b", "failed"), ("c", "skipped or missing")]


def test_compare_passes_an_unchanged_suite_and_more_coverage() -> None:
    compare(_probe(missing=[2]), _probe(missing=[2]))
    compare(_probe(missing=[2, 3]), _probe(missing=[1], statements=2))
    compare(_probe(), _probe({**{t: "passed" for t in TESTS}, "tests/test_app.py::new": "failed"}))
    compare(_probe(measured=False), _probe(measured=False))
    compare(_probe(measured=False), _probe(missing=[1, 2, 3]))


def test_compare_refuses_a_test_that_changed_state_first() -> None:
    after = _probe({TESTS[0]: "error"}, missing=[1, 2, 3], exit_code=1)
    with pytest.raises(BehaviourChangedError) as info:
        compare(_probe(), after)
    assert str(info.value) == (
        f"2 tests passed before the refactor and not after: {TESTS[0]} (error), "
        f"{TESTS[1]} (skipped or missing)"
    )


def test_compare_names_five_tests_and_counts_the_rest() -> None:
    names = [f"t{n}" for n in range(7)]
    with pytest.raises(BehaviourChangedError, match=r"t4 \(failed\) and 2 more$"):
        compare(_probe(dict.fromkeys(names, "passed")), _probe(dict.fromkeys(names, "failed")))


def test_compare_refuses_a_probe_that_exits_non_zero_with_every_test_passing() -> None:
    with pytest.raises(BehaviourChangedError, match="exits with code 3 under the probe"):
        compare(_probe(), _probe(exit_code=3))


def test_compare_refuses_coverage_that_dropped_or_went_unmeasured() -> None:
    with pytest.raises(CoverageDroppedError) as info:
        compare(_probe(missing=[2]), _probe(missing=[2, 3], statements=5))
    assert str(info.value) == (
        "uncovered production statements went from 1 to 2 (75.0% to 60.0% covered)"
    )
    with pytest.raises(CoverageUnmeasuredError) as unmeasured:
        compare(_probe(missing=[2]), _probe(measured=False))
    assert str(unmeasured.value) == (
        "coverage was 1 uncovered of 4 (75.0%) before the refactor and is unmeasured (no report) "
        "after"
    )


def test_a_probe_of_no_statements_is_fully_covered() -> None:
    assert _probe(statements=0).coverage_text() == "0 uncovered of 0 (100.0%)"


def test_after_suite_needs_a_baseline(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="has no baseline"):
        BehaviourCheck().after_suite(tmp_path, FakeSandbox(), {})


# --- a real probe, run on the host


_STATE_OF: Mapping[str, TestState] = {"passed": "passed", "failed": "failed", "error": "error"}


class Local:
    """Runs the sandbox's command over a work tree on the host, with this interpreter for `python`.

    Coverage's own variables are left out of the environment, so the probe's line tracer is the
    only one in the child.
    """

    def check(self) -> None:
        pass

    def cleanup(self, root: Path) -> None:
        pass

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        command = [sys.executable, *argv[1:]] if argv[0] == "python" else list(argv)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_", "COVERAGE"))}
        proc = subprocess.run(command, cwd=root, capture_output=True, env=env, check=False)
        return SandboxResult(tuple(argv), proc.returncode, proc.stdout + proc.stderr, 0.1)


def test_a_real_probe_sees_every_test_pass_and_then_one_break(tmp_path: Path) -> None:
    (fixture,) = build(["refactor-duplicate"], dest=tmp_path / "repos")
    before = probe(fixture.path, Local())
    assert before.exit_code == 0 and before.unmeasured is None
    assert len(before.passed) >= 3 and set(before.states.values()) == {"passed"}
    assert all("::" in test for test in before.passed)
    assert before.uncovered is not None and before.statements

    target = fixture.path / "report" / "lines.py"
    target.write_text(target.read_text().replace("def credit_line(", "def credit_entry("))
    git(fixture.path, "commit", "-q", "-am", "rename")
    after = probe(fixture.path, Local())
    assert after.exit_code != 0
    with pytest.raises(BehaviourChangedError, match="passed before the refactor and not after"):
        compare(before, after)


# --- inside the build loop


class RefactorRunner(BuildRunner):
    """`BuildRunner` whose coder is the refactorer: it writes `rounds[n]` and answers its shape."""

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any] | BaseModel, trace: Trace, **kwargs: Any
    ) -> BaseModel:
        if spec.name != "refactorer":
            return super().__call__(spec, payload, trace, **kwargs)
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        self.calls.append({"spec": spec, "payload": data, **kwargs})
        output = self._refactor(spec, Path(kwargs["repo_path"]), kwargs["hook"])
        trace.record(
            TraceRow(
                run_id=kwargs.get("run_id") or str(uuid.uuid4()),
                pipeline_run_id=kwargs["pipeline_run_id"],
                parent_run_id=kwargs.get("parent_run_id"),
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

    def _refactor(self, spec: LLMSpec, clone: Path, hook: Any) -> dict[str, Any]:
        from runtime.hooks import ToolCall

        number = len(self.coder_calls())
        files = self.rounds[min(number, len(self.rounds)) - 1]
        for path, content in files.items():
            hook.before_tool(
                ToolCall("fs.write_file", {"path": path, "content": content}, spec.address)
            )
            _write(clone, path, content)
        for path, content in self.unhooked.items():
            _write(clone, path, content)
        return {
            "files_changed": [
                {"path": path, "action": "modified", "summary": "Named the message."}
                for path in [*files, *self.unhooked]
            ],
            "commands_run": [],
            "summary": f"Named the message in app.py, round {number}.",
            "behaviour_kept": "Public names unchanged; the suite passes before and after.",
            "signatures_changed": [],
            "blocked": False,
            "blocked_reason": None,
        }

    def coder_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["spec"].name == "refactorer"]


def _refactor_ticket() -> Ticket:
    return Ticket.model_validate(
        {
            "kind": "refactor",
            "title": "Name the greeting",
            "body": "app.py prints a bare string. The standard is a named module constant.",
            "acceptance": ["app.py prints the same text."],
            "repo": FULL,
            "ref": "main",
            "impacted_paths": ["app.py"],
        }
    )


@pytest.fixture(scope="module")
def refactor_agents() -> BuildAgents:
    from pipelines.build_loop import resolve_build_agents

    return resolve_build_agents("refactor", allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh(tmp_path, monkeypatch)
    fake.route("GET", f"repos/{FULL}", REPO_JSON)
    fake.route("POST", f"repos/{FULL}/pulls", PULL_JSON)
    return fake


@pytest.fixture
def repo(tmp_path: Path, gh: FakeGh) -> GithubRepo:
    return github_repo(tmp_path)


def _refactor(
    agents: BuildAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    item_id: str,
    *,
    runner: RefactorRunner,
    sandbox: ScriptedSandbox,
) -> BuildResult:
    return run_build(
        agents,
        item_id,
        trace,
        out_dir=tmp_path / "out",
        sandbox=sandbox,
        runner=runner,
        pipeline_run_id=RUN_ID,
        jev_runner=FakeJev(),
        work_dir=_work_dir(tmp_path),
        repo_path=repo.root,
    )


def test_refactor_tickets_go_to_the_guarded_refactorer_with_the_behaviour_check(
    refactor_agents: BuildAgents,
) -> None:
    assert coder_for("refactor") == ("refactorer", "^0.1")
    assert refactor_agents.coder.address == "refactorer@0.1.0"
    assert refactor_agents.coder.sandbox_profile == "offline"
    assert refactor_agents.coder.guard is not None and refactor_agents.coder.files is not None
    assert KIND_CHECKS["refactor"] is BehaviourCheck
    assert set(KIND_CHECKS) == {"refactor"}


def test_a_refactor_that_keeps_behaviour_ships_with_both_probes_in_the_report(
    refactor_agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace, _refactor_ticket())
    runner = RefactorRunner(rounds=[REFACTORED])
    sandbox = ScriptedSandbox([_output(missing=[2]), _output(missing=[2])])
    before = _branches_tags_and_head(repo.root)
    result = _refactor(
        refactor_agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox
    )

    assert result.code == ExitCode.OK, result.document["stopped"]
    doc = result.to_json()
    assert doc["outcome"] == "shipped" and doc["coder"] == "refactorer@0.1.0"
    ((entry),) = doc["rounds"]
    assert entry["files_changed"] == [{"path": "app.py", "action": "modified"}]
    behaviour = entry["behaviour"]
    assert behaviour["before"]["tests"] == {"passed": 2, "failed": 0, "error": 0}
    assert behaviour["before"]["uncovered"] == behaviour["after"]["uncovered"] == 1
    assert behaviour["tests_changed"] == []
    assert "behaviour held" in result.text()

    # Probe at the head commit, then the round's suite, then the probe of the round's commit.
    argvs = [argv for _, argv, _ in sandbox.runs]
    assert [argv[:2] for argv in argvs] == [("python", "-c"), tuple(PYTEST[:2]), ("python", "-c")]
    assert sandbox.probes == []
    assert len(runner.coder_calls()) == 1
    assert _branches_tags_and_head(repo.root) == before
    report = json.loads((tmp_path / "out" / f"{RUN_ID}.json").read_text(encoding="utf-8"))
    assert report["rounds"][0]["behaviour"] == behaviour
    assert report["total_cost_usd"] == trace.cost_by_pipeline_run(RUN_ID)


def test_a_red_baseline_stops_the_run_before_the_refactorer_runs(
    refactor_agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace, _refactor_ticket())
    runner = RefactorRunner(rounds=[REFACTORED])
    red = _output({TESTS[0]: "passed", TESTS[1]: "failed"}, exit_code=1)
    sandbox = ScriptedSandbox([red])
    result = _refactor(
        refactor_agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox
    )

    doc = _stopped(result, trace, item_id, f"keep behaviour: {TESTS[1]}", "blocked")
    assert doc["stopped"]["type"] == "BaselineNotGreenError"
    assert runner.calls == []
    assert len(sandbox.runs) == 1
    assert list(_work_dir(tmp_path).iterdir()) == []


@pytest.mark.parametrize(
    ("after", "error", "message"),
    [
        (
            _output({TESTS[0]: "passed", TESTS[1]: "failed"}, missing=[2], exit_code=1),
            "BehaviourChangedError",
            f"1 tests passed before the refactor and not after: {TESTS[1]} (failed)",
        ),
        (
            _output(missing=[2, 3]),
            "CoverageDroppedError",
            "uncovered production statements went from 1 to 2 (75.0% to 50.0% covered)",
        ),
        (
            _output(report=False),
            "CoverageUnmeasuredError",
            "is unmeasured (the run printed no coverage report) after",
        ),
    ],
    ids=["test-state", "coverage-dropped", "coverage-unmeasured"],
)
def test_a_refactor_that_changes_behaviour_stops_with_the_item_blocked(
    refactor_agents: BuildAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    gh: FakeGh,
    after: bytes,
    error: str,
    message: str,
) -> None:
    item_id = _ready(trace, _refactor_ticket())
    runner = RefactorRunner(rounds=[REFACTORED])
    sandbox = ScriptedSandbox([_output(missing=[2]), after])
    result = _refactor(
        refactor_agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox
    )

    doc = _stopped(result, trace, item_id, message)
    assert doc["stopped"]["type"] == error
    ((entry),) = doc["rounds"]
    assert entry["suite"]["passed"] and entry["panel"] is None
    assert entry["behaviour"]["after"]["unmeasured"] == (
        "the run printed no coverage report" if error == "CoverageUnmeasuredError" else None
    )
    assert gh.writes() == []
    assert [r for r in trace.pipeline_runs(RUN_ID) if r.agent_ref.startswith("judge@")] == []


def test_a_changed_test_file_the_files_policy_let_through_stops_the_run(
    refactor_agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    # The refactorer's policy denies the test layout; with a policy that lets a test file through,
    # the behaviour check still refuses the round once it is committed, before the suite runs.
    loose = refactor_agents.coder.model_copy(update={"files": FilesSpec(deny=(".git/**",))})
    agents = BuildAgents(coder=loose, panel=refactor_agents.panel)
    item_id = _ready(trace, _refactor_ticket())
    runner = RefactorRunner(rounds=[REFACTORED], unhooked={"tests/data.json": "{}\n"})
    sandbox = ScriptedSandbox([_output()])
    result = _refactor(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox)

    doc = _stopped(result, trace, item_id, "the refactor changed test files: tests/data.json")
    assert doc["stopped"]["type"] == "TestFileChangedError"
    assert [argv[:2] for _, argv, _ in sandbox.runs] == [("python", "-c")]


def test_the_refactorers_own_policy_refuses_a_test_file_first(
    refactor_agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace, _refactor_ticket())
    runner = RefactorRunner(rounds=[REFACTORED], unhooked={"tests/data.json": "{}\n"})
    result = _refactor(
        refactor_agents,
        repo,
        trace,
        tmp_path,
        item_id,
        runner=runner,
        sandbox=ScriptedSandbox([_output()]),
    )
    doc = _stopped(result, trace, item_id, "the coder may not change tests/data.json")
    assert doc["stopped"]["type"] == "FileWriteRefusedError"
    assert WorkItems(trace).get(item_id).status == "escalated"


def test_a_feature_ticket_runs_no_probe(repo: GithubRepo, trace: Trace, tmp_path: Path) -> None:
    from pipelines.build_loop import resolve_build_agents
    from tests.pipelines.test_build_loop import _build

    agents = resolve_build_agents("feature", allow_unpublished=True)
    sandbox = ScriptedSandbox([])
    result = _build(agents, repo, trace, tmp_path, _ready(trace), sandbox=sandbox)
    assert result.code == ExitCode.OK
    assert sandbox.probe_runs() == []
    assert "behaviour" not in result.document["rounds"][0]
    assert "behaviour" not in result.text()
