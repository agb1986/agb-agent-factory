"""The fix loop: write a failing test for one finding, fix the code, test it, and review the fix.

`resolve_fix_agents` pins the agents before anything runs, `run_fix` runs them under one pipeline
run id and `command` is what `factory fix` calls. The flow, all of it in a throwaway clone of the
repository (`pipelines.sandbox.checkout`):

1. `test-writer` writes tests that pin the finding down; its work is committed in the clone.
2. `fixer` changes the code until the suite passes. It may not touch a test: `TestFileGuard`
   refuses a write to a path the test-writer changed, one in the test layout
   (`evals.graders.sandbox_tests.is_test_file`), pytest's configuration at the root of the
   clone, or a root module named like one the runner imports, and a check on the host refuses
   the same paths when they changed by another route, such as a script run through `shell.run`.
   Either refusal writes a trace row tagged `TEST_WRITE_REFUSED_TAG` and ends the run with
   `TestFileWriteError`.
3. The clone is put back to the fixer's commit, untracked and ignored files gone, and the whole
   suite runs in the sandbox; a failure ends the run with `SuiteFailedError`.
4. The review panel (`pipelines.review_panel`) reviews the change from the starting commit to the
   fixed one, with triage and the judge gate as in `factory review`.

When the panel approves, the change is written as a patch next to a JSON report; when it does not,
only the report is written. Nothing is committed to, pushed from or merged into the source
repository. Every run is charged to one `Budget`, and a run that passes it, or an agent that runs
out of turns, ends the loop with that error.
"""

import json
import os
import posixpath
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ValidationError

from evals.graders.sandbox_tests import PYTEST, changed_files, is_test_file
from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from pipelines.ids import new_pipeline_run_id
from pipelines.review_panel import JevRunner, Panel, PanelResult, Runner, resolve_panel, run_panel
from pipelines.sandbox import (
    DockerSandbox,
    Sandbox,
    changed_between,
    checkout,
    clean,
    commit_all,
    diff_patch,
    require_clean,
)
from registry.common_schemas import Finding
from registry.resolver import resolve
from registry.spec import LLMSpec
from registry.work_schemas import FileAction
from runtime.budget import DEFAULT_BUDGET_USD, Budget
from runtime.hooks import ToolCall, ToolDecision, ToolResult
from runtime.jev_runner import run_jev
from runtime.runner import API_KEY_ENV, run
from runtime.trace import Trace, TraceRow
from tools._common import ToolError, resolve_commit, resolve_inside
from tools.sandbox import SandboxResult

TEST_WRITER: tuple[str, str] = ("test-writer", "^0.1")
"""The agent that writes the failing tests and the version range it is pinned to."""

FIXER: tuple[str, str] = ("fixer", "^0.1")
"""The agent that changes the code until the tests pass and the version range it is pinned to."""

TEST_WRITE_REFUSED_TAG = "fix:test_write_refused"
"""The `tag` of the trace row written when the fixer's change to a test is refused."""

DEFAULT_OUT_DIR = Path("data/fixes")
"""Where the patch and the report go, relative to the working directory like the trace database."""

SUITE_TAIL_CHARS = 2000
"""How much of the end of the suite's output the report keeps."""

_GUARDED_TOOL = "fs.write_file"

_RUN_CONFIG = frozenset(
    {
        "pytest.ini",
        ".pytest.ini",
        "pytest.toml",
        ".pytest.toml",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
    }
)
"""The files at the root of the clone that pytest takes its configuration from."""

_RUNNER_MODULES = frozenset({"pytest", "_pytest", "pluggy", "iniconfig", "packaging"})
"""Modules `python -m pytest` imports from the image, which a module or package of the same name at
the root of the clone would replace, since the working directory comes first on `sys.path`.

Best effort, not a complete list: `py` and `pygments`, which pytest also imports, are left out
because a repository may well have a root package of either name, and the panel, which reads the
whole diff, is the real check."""

_MODULE_SUFFIXES = (".py", ".pyc", ".so")


class FixError(FactoryError):
    """The fix loop could not produce a change worth reviewing (exit 1)."""


class TestFileWriteError(FixError):
    """The fixer changed, or tried to change, a test file, which only the test-writer may do."""

    __test__ = False  # not a pytest test class, despite the name


class SuiteFailedError(FixError):
    """The test suite did not pass in the sandbox after the fixer was done."""


@dataclass(frozen=True, slots=True)
class FixAgents:
    """The resolved agents of one fix run: the test-writer, the fixer and the review panel."""

    test_writer: LLMSpec
    fixer: LLMSpec
    panel: Panel


def resolve_fix_agents(
    *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> FixAgents:
    """Resolve the test-writer, the fixer and the full review panel with triage and judge gate.

    Unless `allow_unpublished` is set, only published versions resolve. Raises `UsageError` when
    the test-writer or the fixer is not a sandboxed LLM agent or the fixer has no action guard,
    and the resolver's `NotFoundError` subclasses (exit 3) when an agent or version is missing.
    """
    test_writer = _resolve_worker(*TEST_WRITER, allow_unpublished, trace_path)
    fixer = _resolve_worker(*FIXER, allow_unpublished, trace_path)
    if fixer.guard is None:
        raise UsageError(
            f"{fixer.address} has no action guard, so the fix loop will not run it",
            hint="set `guard` in its manifest",
        )
    panel = resolve_panel(allow_unpublished=allow_unpublished, trace_path=trace_path)
    return FixAgents(test_writer=test_writer, fixer=fixer, panel=panel)


def _resolve_worker(
    name: str, version_range: str, allow_unpublished: bool, trace_path: Path | None
) -> LLMSpec:
    spec = resolve(name, version_range, published_only=not allow_unpublished, trace_path=trace_path)
    if not isinstance(spec, LLMSpec):
        raise UsageError(f"{spec.address} is not an LLM agent, so the fix loop cannot run it")
    if not spec.sandbox:
        raise UsageError(
            f"{spec.address} is not sandboxed, so it cannot write files or run the tests",
            hint="set `sandbox: true` in its manifest",
        )
    return spec


def load_finding(text: str, *, index: int = 0) -> Finding:
    """Return the finding `text` describes: a finding's JSON, or a panel document holding some.

    `text` is JSON, or `@` followed by the path of a file holding it. A document with a `judge`
    key is the output of `factory review --json`, and the finding is entry `index` of the judge's
    merged findings, without its `sources`. Raises `UsageError` for an unreadable file, text that
    is not JSON, a panel document without a judgement or with no entry `index`, and a finding
    that does not match `Finding`.
    """
    if text.startswith("@"):
        path = Path(text[1:])
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError(f"cannot read the finding from {path}: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--finding is not valid JSON: {exc}") from exc
    if isinstance(document, dict) and "judge" in document:
        document = _panel_finding(document, index)
    try:
        return Finding.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "the finding"
        raise UsageError(
            f"--finding is not a finding: {where}: {first['msg']}",
            hint="give file, line_start, line_end, severity, category, title and body",
        ) from exc


def _panel_finding(document: Mapping[str, Any], index: int) -> Any:
    judge = document.get("judge")
    findings = judge.get("output", {}).get("merged_findings") if isinstance(judge, dict) else None
    if not isinstance(findings, list):
        raise UsageError(
            "the panel document has no judgement to take a finding from",
            hint="the judge gate skipped the judge; pass the finding's JSON instead",
        )
    if not 0 <= index < len(findings):
        raise UsageError(
            f"--finding-index {index} is out of range: the judge merged {len(findings)} findings"
        )
    finding = findings[index]
    if isinstance(finding, dict):
        return {key: value for key, value in finding.items() if key != "sources"}
    return finding


class TestFileGuard:
    """The fixer's hook: refuse every `fs.write_file` to a test, before the action guard sees it.

    A path is a test when `evals.graders.sandbox_tests.is_test_file` says so or it is in
    `protected`, the paths the test-writer changed. Files that control the test run count too:
    pytest's configuration files at the root of the clone, and a root module or package named
    like one `python -m pytest` imports, which would replace it. The path is checked as written,
    after `.` and `..` are folded, and again after symlinks are resolved inside `clone`; either
    match refuses it. A path that does not resolve is let through, and `fs.write_file` refuses it
    itself. A refusal writes a row tagged `TEST_WRITE_REFUSED_TAG` and raises `TestFileWriteError`,
    which ends the fixer's run. Every other tool is allowed and left to the action guard.
    """

    __test__ = False  # not a pytest test class, despite the name

    def __init__(
        self, clone: Path, protected: frozenset[str], trace: Trace, pipeline_run_id: str
    ) -> None:
        self.clone = clone
        self.protected = protected
        self.trace = trace
        self.pipeline_run_id = pipeline_run_id

    def before_tool(self, call: ToolCall) -> ToolDecision:
        """Raise `TestFileWriteError` for a write to a test; allow everything else."""
        if call.name != _GUARDED_TOOL:
            return ToolDecision.allow()
        path = call.arguments.get("path")
        if not isinstance(path, str):
            return ToolDecision.allow()
        for candidate in _candidates(self.clone, path):
            reason = _test_reason(candidate, self.protected)
            if reason is not None:
                raise _refuse(
                    self.trace,
                    call.agent,
                    self.pipeline_run_id,
                    path=candidate,
                    reason=reason,
                    check="hook",
                )
        return ToolDecision.allow()

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        """Do nothing: the host check after the run catches what a hook cannot see."""


def _candidates(clone: Path, path: str) -> list[str]:
    """Return `path` relative to `clone` as written and as resolved, without repeats."""
    found: list[str] = []
    lexical = PurePosixPath(posixpath.normpath(path))
    if lexical.is_absolute():
        lexical = (
            lexical.relative_to(clone.as_posix())
            if lexical.is_relative_to(clone.as_posix())
            else PurePosixPath(".")
        )
    if lexical.parts and lexical.parts[0] != "..":
        found.append(lexical.as_posix())
    try:
        resolved = resolve_inside(clone, path).as_posix()
    except ToolError:
        resolved = None
    if resolved is not None and resolved not in found:
        found.append(resolved)
    return [candidate for candidate in found if candidate != "."]


def _test_reason(path: str, protected: frozenset[str]) -> str | None:
    if path in protected:
        return "the test-writer changed this path"
    if is_test_file(path):
        return "the path is in the test layout"
    parts = PurePosixPath(path).parts
    if len(parts) == 1 and parts[0] in _RUN_CONFIG:
        return "the file configures the test run"
    if parts and parts[0] in _RUNNER_MODULES and len(parts) > 1:
        return "the path shadows the test runner"
    if (
        len(parts) == 1
        and parts[0].split(".", 1)[0] in _RUNNER_MODULES
        and parts[0].endswith(_MODULE_SUFFIXES)
    ):
        return "the path shadows the test runner"
    return None


def _refuse(
    trace: Trace, agent: str, pipeline_run_id: str, *, path: str, reason: str, check: str
) -> TestFileWriteError:
    """Write the refusal row for the fixer's change to `path` and return the error to raise."""
    message = f"the fixer may not change the test file {path}: {reason}"
    trace.record(
        TraceRow(
            run_id=str(uuid.uuid4()),
            agent_ref=agent,
            kind="llm",
            started_at=datetime.now(UTC),
            duration_ms=0,
            input_json={"tool": _GUARDED_TOOL, "path": path},
            pipeline_run_id=pipeline_run_id,
            cost_usd=0.0,
            decision_json={"allow": False, "path": path, "reason": reason, "check": check},
            error=message,
            tag=TEST_WRITE_REFUSED_TAG,
        )
    )
    return TestFileWriteError(
        message, hint="tests belong to the test-writer; the fixer changes the code under test"
    )


@dataclass(frozen=True, slots=True)
class FixResult:
    """Everything one fix run produced. `to_json` is the report `factory fix` writes and prints."""

    pipeline_run_id: str
    repo_path: str
    head: str
    finding: Finding
    test_writer: LLMSpec
    test_writer_output: BaseModel
    fixer: LLMSpec
    fixer_output: BaseModel
    files_changed: tuple[tuple[str, FileAction, tuple[str, ...]], ...]
    suite: SandboxResult
    panel: PanelResult
    patch_path: Path | None
    report_path: Path
    total_cost_usd: float
    duration_ms: int

    @property
    def ship(self) -> bool:
        """Whether the panel approved the change, and so whether a patch was written."""
        return self.panel.ship

    def to_json(self) -> dict[str, Any]:
        """Return the report: the finding, both agents' answers, the files, suite and panel."""
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "repo_path": self.repo_path,
            "head": self.head,
            "finding": self.finding.model_dump(mode="json"),
            "test_writer": {
                "ref": self.test_writer.address,
                "output": self.test_writer_output.model_dump(mode="json"),
            },
            "fixer": {
                "ref": self.fixer.address,
                "output": self.fixer_output.model_dump(mode="json"),
            },
            "files_changed": [
                {"path": path, "action": action, "by": list(by)}
                for path, action, by in self.files_changed
            ],
            "suite": {
                "argv": list(self.suite.argv),
                "exit_code": self.suite.exit_code,
                "passed": self.suite.exit_code == 0,
                "seconds": round(self.suite.seconds, 3),
                "output_tail": _tail(self.suite.output),
            },
            "panel": self.panel.to_json(),
            "ship": self.ship,
            "patch_path": str(self.patch_path) if self.patch_path is not None else None,
            "report_path": str(self.report_path),
            "total_cost_usd": self.total_cost_usd,
            "duration_ms": self.duration_ms,
        }

    def text(self) -> str:
        """Return a short plain-text report of the run, for use without `--json`."""
        where = f"{self.finding.file}:{self.finding.line_start}"
        lines = [f"fix run {self.pipeline_run_id}: {where} {self.finding.title}"]
        for path, action, by in self.files_changed:
            lines.append(f"  {action} {path} ({', '.join(by)})")
        lines.append(f"  suite passed in {self.suite.seconds:.1f}s")
        lines.append("panel:")
        lines.extend(f"  {line}" for line in self.panel.text().splitlines())
        if self.patch_path is not None:
            lines.append(f"patch {self.patch_path}")
        else:
            lines.append("no patch: the panel did not approve the change")
        lines.append(f"report {self.report_path}")
        lines.append(f"cost ${self.total_cost_usd:.4f}")
        return "\n".join(lines)


def _tail(output: bytes) -> str:
    text = output.decode("utf-8", errors="replace")
    return text[-SUITE_TAIL_CHARS:]


def run_fix(
    agents: FixAgents,
    repo_path: str | Path,
    finding: Finding,
    trace: Trace,
    *,
    head: str = "HEAD",
    out_dir: str | Path = DEFAULT_OUT_DIR,
    work_dir: str | Path | None = None,
    sandbox: Sandbox | None = None,
    runner: Runner = run,
    jev_runner: JevRunner = run_jev,
    pipeline_run_id: str | None = None,
    budget: Budget | None = None,
) -> FixResult:
    """Fix `finding` in the repository at `repo_path`, starting from the commit `head`.

    The repository must have nothing uncommitted (`DirtyRepositoryError`, exit 2 otherwise). The
    agents work in a clone under `work_dir` (default: the system temporary directory), which is
    deleted at the end, and `sandbox` (default: `DockerSandbox`) runs the suite and is told to
    stop whatever still runs over the clone, both however the run ends. Every agent call gets
    the same `pipeline_run_id` and is charged to `budget` (default: `DEFAULT_BUDGET_USD`).

    The patch, `<pipeline run id>.patch`, is written to `out_dir` only when the panel approves;
    the report, `<pipeline run id>.json`, is written whenever the panel ran. The fixer reporting
    `nothing_to_fix` does not stop the run: whatever the two agents changed, tests alone
    included, goes to the panel. Raises `UsageError` for a `work_dir` that is not a directory or
    whose path has a `:`, which Docker cannot mount, or an `out_dir` that cannot be made, the
    sandbox's `ToolUnavailableError` before any agent runs,
    `TestFileWriteError` when the fixer changes a test, `FixError` when neither agent changed
    anything, `SuiteFailedError` when the suite fails, and whatever an agent's run or the panel
    raises, `BudgetExceeded` among them.
    """
    start = time.monotonic()
    run_id = pipeline_run_id or new_pipeline_run_id()
    budget = budget or Budget(DEFAULT_BUDGET_USD)
    sandbox = sandbox or DockerSandbox()
    if work_dir is not None and not Path(work_dir).is_dir():
        raise UsageError(f"--work-dir {work_dir} is not a directory")
    where = Path(work_dir or tempfile.gettempdir()).resolve()
    if ":" in str(where):
        raise UsageError(
            f"the work directory {where} has a ':' in its path, which Docker cannot mount",
            hint="pass --work-dir a directory whose path has no ':'",
        )
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"cannot make the output directory {out}: {exc}") from exc
    sandbox.check()
    root = require_clean(repo_path)
    head_sha = resolve_commit(root, head)
    with checkout(root, head_sha, work_dir=work_dir) as clone:
        try:
            tests_output = runner(
                agents.test_writer,
                agents.test_writer.input_class.model_validate(
                    {"repo_path": str(clone), "finding": finding.model_dump(mode="json")}
                ),
                trace,
                repo_path=clone,
                pipeline_run_id=run_id,
                budget=budget,
            )
            tests_sha = commit_all(clone, f"test-writer: {finding.title}")
            by_tests = changed_between(clone, head_sha, tests_sha)
            written = tests_output.model_dump(mode="json")
            protected = frozenset(by_tests)

            fixer_output = runner(
                agents.fixer,
                agents.fixer.input_class.model_validate(
                    {
                        "repo_path": str(clone),
                        "finding": finding.model_dump(mode="json"),
                        "test_files": written.get("test_files", []),
                    }
                ),
                trace,
                repo_path=clone,
                pipeline_run_id=run_id,
                hook=TestFileGuard(clone, protected, trace, run_id),
                budget=budget,
            )
            # `changed_files` leaves out bytecode and `__pycache__`. Nothing is lost by that: the
            # commits carry no bytecode either, and `clean` deletes it before the suite runs.
            for path in changed_files(clone):
                reason = _test_reason(path, protected)
                if reason is not None:
                    raise _refuse(
                        trace,
                        agents.fixer.address,
                        run_id,
                        path=path,
                        reason=reason,
                        check="post-check",
                    )
            fix_sha = commit_all(clone, f"fixer: {finding.title}")
            by_fixer = changed_between(clone, tests_sha, fix_sha)
            total = changed_between(clone, head_sha, fix_sha)
            if not total:
                raise FixError(
                    "the test-writer and the fixer changed nothing",
                    hint="check the finding still applies at the head commit",
                )

            clean(clone)
            suite = sandbox.run(clone, PYTEST)
            if suite.exit_code != 0:
                raise SuiteFailedError(
                    f"the test suite failed in the sandbox with exit code {suite.exit_code}",
                    hint=_tail(suite.output)[-400:].strip() or None,
                )

            panel = run_panel(
                agents.panel,
                clone,
                head_sha,
                fix_sha,
                trace,
                runner=runner,
                jev_runner=jev_runner,
                pipeline_run_id=run_id,
                budget=budget,
            )
            patch_path = None
            if panel.ship:
                patch_path = out / f"{run_id}.patch"
                patch_path.write_bytes(diff_patch(clone, head_sha, fix_sha))
        finally:
            sandbox.cleanup(clone)

    files = tuple(
        (
            path,
            action,
            tuple(
                agent.name
                for agent, changed in ((agents.test_writer, by_tests), (agents.fixer, by_fixer))
                if path in changed
            ),
        )
        for path, action in total.items()
    )
    result = FixResult(
        pipeline_run_id=run_id,
        repo_path=str(root),
        head=head_sha,
        finding=finding,
        test_writer=agents.test_writer,
        test_writer_output=tests_output,
        fixer=agents.fixer,
        fixer_output=fixer_output,
        files_changed=files,
        suite=suite,
        panel=panel,
        patch_path=patch_path,
        report_path=out / f"{run_id}.json",
        total_cost_usd=trace.cost_by_pipeline_run(run_id),
        duration_ms=round((time.monotonic() - start) * 1000),
    )
    result.report_path.write_text(json.dumps(result.to_json(), indent=2) + "\n", encoding="utf-8")
    return result


def command(
    repo_path: str,
    *,
    finding: str,
    finding_index: int = 0,
    head: str = "HEAD",
    out_dir: str | None = None,
    work_dir: str | None = None,
    budget_usd: float | None = None,
    as_json: bool = False,
    allow_unpublished: bool = False,
) -> int:
    """Run `factory fix`: fix one finding in `repo_path`, review the fix and write the patch.

    `finding` is a finding's JSON, a `factory review --json` document (the finding is its judge's
    merged finding `finding_index`), or either one in a file named `@path`. Prose goes to stderr
    under `as_json`, where stdout gets exactly one JSON document, the report, also when the panel
    does not approve. Without it the text report goes to stdout. Returns 0 when the panel approves
    the fix and 1 when it does not. `budget_usd` caps the spend of the whole run (default:
    `DEFAULT_BUDGET_USD`); passing it raises `BudgetExceeded`, exit 1, with the spend so far.
    """
    parsed = load_finding(finding, index=finding_index)
    agents = resolve_fix_agents(allow_unpublished=allow_unpublished)
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(
            f"{API_KEY_ENV} is not set",
            hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
        )
    budget = Budget(DEFAULT_BUDGET_USD if budget_usd is None else budget_usd)
    with Trace() as trace:
        result = run_fix(
            agents,
            repo_path,
            parsed,
            trace,
            head=head,
            out_dir=DEFAULT_OUT_DIR if out_dir is None else out_dir,
            work_dir=work_dir,
            runner=run,
            jev_runner=run_jev,
            budget=budget,
        )
    if as_json:
        print(result.text(), file=sys.stderr)
        print(json.dumps(result.to_json()))
    else:
        print(result.text())
    return ExitCode.OK if result.ship else ExitCode.ERROR
