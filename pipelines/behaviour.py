"""The behaviour check the build loop runs for `refactor` tickets: a refactor must not change behaviour.

`BehaviourCheck` is the build loop's per-kind hook for `refactor` (`pipelines.build_loop.KIND_CHECKS`).
It probes the whole suite twice in the sandbox, once at the ticket's ref before the coder runs and
once after each round's suite has passed, and compares the two:

- `before` probes the clone at the head commit. A suite that does not pass there, or in which no
  test passes, ends the run with `BaselineNotGreenError`: a refactor is judged against tests that
  pin today's behaviour, and a red or empty suite pins nothing.
- `after_commit` refuses a round whose commit changed a test file (`TestFileChangedError`), by the
  rule of `evals.graders.sandbox_tests.is_test_file`. The coder's `files` policy already denies the
  usual test paths; this catches the rest, such as a data file under `tests/`.
- `after_suite` probes the round's commit. A test that passed before and fails, errors, is skipped
  or is gone after ends the run with `BehaviourChangedError`, naming the tests. When the number of
  uncovered statements in production files went up it ends with `CoverageDroppedError`, naming both
  figures; when coverage was measured before but cannot be after it ends with
  `CoverageUnmeasuredError`.

A probe is one sandbox command: `python -c` with `evals.coverage.MEASURE_SCRIPT`, which runs pytest
in process under the standard library's line tracer, so the image needs no plugin. Pytest runs with
`-rpfE --tb=no`, so its short summary lists every passed, failed and erroring test by node id, which
is how the per-test states are read. The coverage targets are the tracked `.py` files that are not
tests, read on the host with `git ls-files`; they travel inside the script, not as arguments, since
the sandbox allows 64 arguments. Coverage is compared by the count of uncovered statements, not by
the ratio, because removing duplicate code removes covered lines and so lowers the ratio of a change
that loses nothing.

Coverage is unmeasured, and the coverage rule skipped, when the baseline has no production `.py`
file, when the list of them does not fit in one sandbox argument (`MAX_SCRIPT_CHARS`), or when the
baseline run printed no coverage report. The test-state rule always applies.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from evals.coverage import MEASURE_SCRIPT, CoverageError, parse_report
from evals.graders.sandbox_tests import is_test_file
from factory.exit import FactoryError
from pipelines.sandbox import Sandbox, clean
from registry.work_schemas import CoverageReport, FileAction
from tools._common import ToolExecutionError, fail_hint, git
from tools.sandbox import SandboxResult

type TestState = Literal["passed", "failed", "error"]
"""What pytest's short summary says of one test; a test it does not list passed or failed was
skipped, deselected or not collected."""

PROBE_PYTEST_ARGS: tuple[str, ...] = ("-q", "-rpfE", "--tb=no", "-p", "no:cacheprovider")
"""Pytest's arguments in a probe: a short summary line per passed, failed and erroring test."""

MAX_SCRIPT_CHARS = 32_768
"""The longest probe script, the sandbox's limit on one argument (`tools.sandbox.check_argv`)."""

SUMMARY_HEADER = "short test summary info"
"""What the line that starts pytest's short summary contains."""

_STATES: Mapping[str, TestState] = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error"}
_SHOWN = 5


class BehaviourError(FactoryError):
    """A refactor changed behaviour, or the build loop could not show that it did not (exit 1)."""


class BaselineNotGreenError(BehaviourError):
    """The suite does not pass at the ticket's ref, so no change could be shown to keep behaviour."""


class TestFileChangedError(BehaviourError):
    """The refactor changed a test file, so the suite no longer pins the behaviour it had."""

    __test__ = False  # not a pytest test class, despite the name


class BehaviourChangedError(BehaviourError):
    """A test that passed before the refactor does not pass after it."""


class CoverageDroppedError(BehaviourError):
    """The refactor left more production statements uncovered than there were before."""


class CoverageUnmeasuredError(BehaviourError):
    """Coverage was measured before the refactor and could not be measured after it."""


@dataclass(frozen=True, slots=True)
class Probe:
    """What one probe of the suite found: how pytest exited, each test's state and the coverage.

    `coverage` is None when it was not measured, and `unmeasured` then says why.
    """

    exit_code: int
    states: dict[str, TestState]
    coverage: CoverageReport | None
    unmeasured: str | None
    seconds: float

    @property
    def passed(self) -> list[str]:
        """The node ids of the tests that passed."""
        return [test for test, state in self.states.items() if state == "passed"]

    @property
    def statements(self) -> int | None:
        """How many statements the production files have, or None when unmeasured."""
        return None if self.coverage is None else sum(f.statements for f in self.coverage.files)

    @property
    def uncovered(self) -> int | None:
        """How many of them no test ran, or None when unmeasured."""
        if self.coverage is None:
            return None
        return sum(len(f.missing_lines) for f in self.coverage.files)

    def coverage_text(self) -> str:
        """Return the coverage as `<uncovered> uncovered of <statements> (<percent>%)`."""
        if self.statements is None or self.uncovered is None:
            return f"unmeasured ({self.unmeasured})"
        return f"{self.uncovered} uncovered of {self.statements} ({_percent(self)})"

    def to_json(self) -> dict[str, Any]:
        """Return the probe as JSON-ready data for the build report, without the per-test list."""
        counts = {state: 0 for state in _STATES.values()}
        for state in self.states.values():
            counts[state] += 1
        return {
            "exit_code": self.exit_code,
            "tests": counts,
            "statements": self.statements,
            "uncovered": self.uncovered,
            "unmeasured": self.unmeasured,
            "seconds": round(self.seconds, 3),
        }


def _percent(probe: Probe) -> str:
    statements, uncovered = probe.statements or 0, probe.uncovered or 0
    if statements == 0:
        return "100.0%"
    return f"{100 * (statements - uncovered) / statements:.1f}%"


def parse_states(output: bytes) -> dict[str, TestState]:
    """Return each test's state from pytest's short summary in `output`, by node id.

    Only the lines after the last summary header count, so captured output cannot pose as one. A
    `PASSED` line is the node id alone, which may hold spaces; a `FAILED` or `ERROR` line is the
    node id, ` - ` and a message. A collection error's node id is the file's path.
    """
    lines = output.decode("utf-8", errors="replace").splitlines()
    starts = [index for index, line in enumerate(lines) if SUMMARY_HEADER in line]
    states: dict[str, TestState] = {}
    if not starts:
        return states
    for line in lines[starts[-1] + 1 :]:
        word, _, rest = line.partition(" ")
        if word not in _STATES or not rest.strip():
            continue
        test = rest.strip() if word == "PASSED" else rest.split(" - ", 1)[0].strip()
        states[test] = _STATES[word]
    return states


def production_files(clone: Path) -> list[str]:
    """Return the tracked `.py` files of `clone` that are not tests, sorted.

    Raises `ToolExecutionError` when git cannot list them.
    """
    proc = git(["ls-files", "-z"], clone)
    if proc.returncode != 0:
        raise ToolExecutionError("git ls-files failed in the checkout", hint=fail_hint(proc))
    paths = proc.stdout.decode("utf-8", errors="replace").split("\0")
    return sorted(p for p in paths if p.endswith(".py") and not is_test_file(p))


def probe_script(targets: Sequence[str]) -> str:
    """Return the `python -c` script that measures `targets` over a run with `PROBE_PYTEST_ARGS`."""
    args = json.dumps([*targets, "--", *PROBE_PYTEST_ARGS])
    return f"import json, sys\nsys.argv = ['-c', *json.loads({args!r})]\n{MEASURE_SCRIPT}"


def probe(clone: Path, sandbox: Sandbox) -> Probe:
    """Run the whole suite of `clone` once in `sandbox` under the line tracer; return what it found.

    Raises `ToolExecutionError` when git cannot list the files, and whatever `sandbox.run` raises.
    """
    targets = production_files(clone)
    unmeasured: str | None = None
    if not targets:
        unmeasured = "no production .py file is tracked"
    elif len(probe_script(targets)) > MAX_SCRIPT_CHARS:
        unmeasured = f"the {len(targets)} production files do not fit in one sandbox argument"
        targets = []
    result = sandbox.run(clone, ["python", "-c", probe_script(targets)])
    coverage, exit_code = _coverage(result)
    if coverage is None:
        unmeasured = unmeasured or "the run printed no coverage report"
    elif not targets:
        coverage = None
    return Probe(exit_code, parse_states(result.output), coverage, unmeasured, result.seconds)


def _coverage(result: SandboxResult) -> tuple[CoverageReport | None, int]:
    try:
        report = parse_report(result)
    except CoverageError:
        return None, result.exit_code
    return report, report.pytest_exit_code


def require_green(baseline: Probe, head_sha: str) -> None:
    """Raise `BaselineNotGreenError` unless the suite passed at `head_sha` with a test passing."""
    if baseline.exit_code != 0:
        failing = [test for test, state in baseline.states.items() if state != "passed"]
        named = f": {_names(failing)}" if failing else ""
        raise BaselineNotGreenError(
            f"the suite does not pass at {head_sha[:12]} (exit code {baseline.exit_code}), so a "
            f"refactor cannot be shown to keep behaviour{named}",
            hint="fix the suite first, or file the work as a bug",
        )
    if not baseline.passed:
        raise BaselineNotGreenError(
            f"no test passes at {head_sha[:12]}, so nothing pins the behaviour a refactor keeps",
            hint="add tests that pin the behaviour first",
        )


def require_no_test_change(changed: Mapping[str, FileAction]) -> None:
    """Raise `TestFileChangedError` when a path in `changed` is a test file."""
    tests = sorted(path for path in changed if is_test_file(path))
    if tests:
        raise TestFileChangedError(
            f"the refactor changed test files: {_names(tests)}",
            hint="a refactor keeps the tests as they are; they are what shows behaviour held",
        )


def changed_tests(before: Probe, after: Probe) -> list[tuple[str, str]]:
    """Return each test that passed in `before` and did not in `after`, with what it did after."""
    return [
        (test, after.states.get(test, "skipped or missing"))
        for test in before.passed
        if after.states.get(test) != "passed"
    ]


def compare(before: Probe, after: Probe) -> None:
    """Raise the `BehaviourError` that says how `after` differs from `before`, if one does.

    The test states come first, then pytest's exit code, then the coverage.
    """
    moved = changed_tests(before, after)
    if moved:
        shown = [f"{test} ({state})" for test, state in moved]
        raise BehaviourChangedError(
            f"{len(moved)} tests passed before the refactor and not after: {_names(shown)}",
            hint="the refactor changed behaviour; read the suite output in the report",
        )
    if after.exit_code != 0:
        raise BehaviourChangedError(
            f"the suite exits with code {after.exit_code} under the probe after the refactor",
            hint="the suite passed alone; the probe runs it in process under a line tracer",
        )
    if before.uncovered is None:
        return
    if after.uncovered is None:
        raise CoverageUnmeasuredError(
            f"coverage was {before.coverage_text()} before the refactor and is "
            f"{after.coverage_text()} after"
        )
    if after.uncovered > before.uncovered:
        raise CoverageDroppedError(
            f"uncovered production statements went from {before.uncovered} to {after.uncovered} "
            f"({_percent(before)} to {_percent(after)} covered)",
            hint="the refactor added code no test runs; keep the change to code the suite covers",
        )


def _names(items: Sequence[str]) -> str:
    more = f" and {len(items) - _SHOWN} more" if len(items) > _SHOWN else ""
    return ", ".join(items[:_SHOWN]) + more


class KindCheck(Protocol):
    """Checks the build loop adds for one ticket kind, one instance per run.

    `before` runs in the clone at the head commit before the coder's first round, `after_commit`
    once a round is committed, with what the round changed, and `after_suite` once the round's
    suite has passed and before the panel, with the round's report entry to record into. Each
    raises a `FactoryError` to stop the run, which leaves the item blocked.
    """

    def before(self, clone: Path, sandbox: Sandbox, head_sha: str) -> None: ...

    def after_commit(self, changed: Mapping[str, FileAction]) -> None: ...

    def after_suite(self, clone: Path, sandbox: Sandbox, entry: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class BehaviourCheck:
    """The `refactor` hook: the suite's per-test states and coverage must hold (module docstring)."""

    baseline: Probe | None = field(default=None)

    def before(self, clone: Path, sandbox: Sandbox, head_sha: str) -> None:
        """Probe the suite at the head commit and refuse unless it is green."""
        self.baseline = probe(clone, sandbox)
        clean(clone)
        require_green(self.baseline, head_sha)

    def after_commit(self, changed: Mapping[str, FileAction]) -> None:
        """Refuse a round that changed a test file."""
        require_no_test_change(changed)

    def after_suite(self, clone: Path, sandbox: Sandbox, entry: dict[str, Any]) -> None:
        """Probe the round's commit, record both probes in `entry` and refuse a change."""
        if self.baseline is None:
            raise BehaviourError("the behaviour check has no baseline; `before` did not run")
        after = probe(clone, sandbox)
        clean(clone)
        entry["behaviour"] = {
            "before": self.baseline.to_json(),
            "after": after.to_json(),
            "tests_changed": [
                {"test": test, "after": state}
                for test, state in changed_tests(self.baseline, after)
            ],
        }
        compare(self.baseline, after)


__all__ = [
    "MAX_SCRIPT_CHARS",
    "PROBE_PYTEST_ARGS",
    "BaselineNotGreenError",
    "BehaviourChangedError",
    "BehaviourCheck",
    "BehaviourError",
    "CoverageDroppedError",
    "CoverageUnmeasuredError",
    "KindCheck",
    "Probe",
    "TestFileChangedError",
    "changed_tests",
    "compare",
    "parse_states",
    "probe",
    "probe_script",
    "production_files",
    "require_green",
    "require_no_test_change",
]
