"""Line coverage of chosen source files, measured in the sandbox with the standard library alone.

The sandbox image has pytest but not coverage.py, and it has no network to install it, so
`measure_coverage` sends a short script (`MEASURE_SCRIPT`) to `python -c`. The script starts a
line tracer, `sys.monitoring` where the interpreter has it (3.12 and later) and `sys.settrace`
before that, runs pytest in the same process, and prints one JSON line after `REPORT_MARKER`.

A line counts as a statement when the compiled module has bytecode for it, which is what
`code.co_lines()` reports over the module and every code object nested in it. A file that the run
never imports has all of its statements missing. The measure is line coverage, not branch
coverage: a line that runs counts as covered even when only one side of a condition on it ran,
such as `x if flag else y` or an `if` with no `else` whose condition was always true.

`coverage_delta` compares two reports of the same files, and `restore_head` puts a copy of a work
tree back to its HEAD commit, which is how the `sandbox_tests` grader measures the state before an
agent wrote its tests.
"""

from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError

from registry.work_schemas import CoverageReport, FileCoverage
from tools._common import (
    ToolError,
    ToolExecutionError,
    ToolInputError,
    git,
    resolve_inside,
    resolve_root,
)
from tools.sandbox import SandboxResult, run_in_sandbox

__all__ = [
    "MEASURE_SCRIPT",
    "REPORT_MARKER",
    "CoverageDelta",
    "CoverageError",
    "CoverageReport",
    "FileCoverage",
    "check_targets",
    "coverage_argv",
    "coverage_delta",
    "measure_coverage",
    "parse_report",
    "restore_head",
]

REPORT_MARKER = "@@factory-coverage@@ "
"""What the report line starts with, so it can be told apart from pytest's own output."""

MEASURE_SCRIPT = r"""
import json, os, sys, types

MARKER = "@@factory-coverage@@ "


def statements(path):
    try:
        with open(path, "rb") as handle:
            code = compile(handle.read(), path, "exec", dont_inherit=True)
    except (OSError, SyntaxError, ValueError):
        return set()
    lines, stack = set(), [code]
    while stack:
        current = stack.pop()
        lines.update(line for _, _, line in current.co_lines() if line)
        stack.extend(k for k in current.co_consts if isinstance(k, types.CodeType))
    return lines


def main():
    args = sys.argv[1:]
    cut = args.index("--") if "--" in args else len(args)
    targets, pytest_args = args[:cut], args[cut + 1 :]
    root = os.getcwd()
    keys = {os.path.realpath(os.path.join(root, t)): t for t in targets}
    hits = {t: set() for t in targets}
    seen = {}

    def lookup(filename):
        if filename not in seen:
            seen[filename] = keys.get(os.path.realpath(os.path.join(root, filename)))
        target = seen[filename]
        return None if target is None else hits[target]

    import pytest

    monitoring = getattr(sys, "monitoring", None)
    if monitoring is not None:
        free = [i for i in (monitoring.COVERAGE_ID, 3, 4) if monitoring.get_tool(i) is None]
        tool = free[0] if free else monitoring.COVERAGE_ID
        monitoring.use_tool_id(tool, "factory-coverage")

        def on_line(code, line):
            found = lookup(code.co_filename)
            if found is not None:
                found.add(line)
            return monitoring.DISABLE

        monitoring.register_callback(tool, monitoring.events.LINE, on_line)
        monitoring.set_events(tool, monitoring.events.LINE)
    else:
        import threading

        def tracer(frame, event, arg):
            found = lookup(frame.f_code.co_filename)
            if found is None:
                return None

            def local(frame, event, arg):
                if event == "line":
                    found.add(frame.f_lineno)
                return local

            return local

        sys.settrace(tracer)
        threading.settrace(tracer)
    try:
        exit_code = int(pytest.main(pytest_args))
    finally:
        if monitoring is not None:
            monitoring.set_events(tool, 0)
            monitoring.free_tool_id(tool)
        else:
            sys.settrace(None)
    files = []
    for target in targets:
        lines = statements(os.path.join(root, target))
        missing = sorted(lines - hits[target])
        files.append(
            {
                "path": target,
                "statements": len(lines),
                "covered": len(lines) - len(missing),
                "missing_lines": missing,
            }
        )
    sys.stdout.flush()
    print(MARKER + json.dumps({"files": files, "pytest_exit_code": exit_code}), flush=True)
    sys.exit(exit_code)


main()
"""
"""The script `python -c` runs: the arguments are the target paths, `--`, then pytest's."""

type Run = Callable[[Path, Sequence[str]], SandboxResult]
"""How a command runs over a work tree: `tools.sandbox.run_in_sandbox`, or a pipeline's sandbox."""

_TAIL_CHARS = 400


class CoverageError(ToolExecutionError):
    """The coverage run produced no report, such as when pytest could not start."""


class CoverageDelta(BaseModel):
    """What changed between two coverage reports of the same files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    newly_covered: dict[str, list[int]]
    """Per path, the lines missing before and covered after, for paths with any."""
    still_missing: dict[str, list[int]]
    """Per path, the lines missing after, for paths with any."""

    @property
    def gained(self) -> int:
        """How many lines became covered, over all the files."""
        return sum(len(lines) for lines in self.newly_covered.values())

    @property
    def missing(self) -> int:
        """How many lines are still missing, over all the files."""
        return sum(len(lines) for lines in self.still_missing.values())


def coverage_argv(targets: Sequence[str], pytest_args: Sequence[str] = ("-q",)) -> list[str]:
    """Return the sandbox command that measures `targets` over a pytest run with `pytest_args`."""
    return ["python", "-c", MEASURE_SCRIPT, *targets, "--", *pytest_args]


def check_targets(root: Path, targets: Sequence[str]) -> list[str]:
    """Return `targets` as POSIX paths relative to `root`, after checking each is a Python file.

    `root` must come from `tools._common.resolve_root`.
    Raises `ToolInputError` for an empty list, a repeat, a path outside the repository or one
    that is not a `.py` file in it.
    """
    if not targets:
        raise ToolInputError("no target paths to measure")
    checked: list[str] = []
    for target in targets:
        try:
            rel = resolve_inside(root, target)
        except ToolError as exc:
            raise ToolInputError(f"target path {target!r}: {exc.message}") from exc
        if rel.suffix != ".py" or not (root / rel).is_file():
            raise ToolInputError(f"target path {target!r} is not a Python file in the repository")
        if rel.as_posix() in checked:
            raise ToolInputError(f"target path {target!r} is given twice")
        checked.append(rel.as_posix())
    return checked


def measure_coverage(
    root: str | Path,
    targets: Sequence[str],
    *,
    pytest_args: Sequence[str] = ("-q",),
    run: Run = run_in_sandbox,
) -> CoverageReport:
    """Run pytest with `pytest_args` over the work tree `root` and return the coverage of `targets`.

    `run` runs the command, in the sandbox by default. The report says how pytest exited, and
    failing tests do not stop the measure. Raises the `resolve_root` errors for a path that is not
    a work tree, `ToolInputError` for a bad target path,
    `CoverageError` when the run prints no report, and whatever `run` raises, such as
    `ToolUnavailableError` without Docker.
    """
    where = resolve_root(root)
    checked = check_targets(where, targets)
    return parse_report(run(where, coverage_argv(checked, pytest_args)))


def parse_report(result: SandboxResult) -> CoverageReport:
    """Return the report in the output of a coverage run; raise `CoverageError` when it has none."""
    text = result.output.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.startswith(REPORT_MARKER)]
    if not lines:
        tail = " ".join(text.split())[-_TAIL_CHARS:]
        raise CoverageError(
            f"the coverage run printed no report (exit code {result.exit_code})", hint=tail or None
        )
    try:
        return CoverageReport.model_validate_json(lines[-1][len(REPORT_MARKER) :])
    except ValidationError as exc:
        raise CoverageError(f"the coverage report is malformed: {exc.errors()[0]['msg']}") from exc


def coverage_delta(before: CoverageReport, after: CoverageReport) -> CoverageDelta:
    """Return the lines `after` covers that `before` did not, and what `after` still misses.

    The reports must name the same paths, and the files must not have changed in between, since
    the comparison is by line number. Raises `ValueError` when the paths differ.
    """
    missing_before = {entry.path: set(entry.missing_lines) for entry in before.files}
    missing_after = {entry.path: set(entry.missing_lines) for entry in after.files}
    if missing_before.keys() != missing_after.keys():
        raise ValueError(
            f"the reports cover different paths: {sorted(missing_before)} and {sorted(missing_after)}"
        )
    newly = {
        path: sorted(lines - missing_after[path])
        for path, lines in missing_before.items()
        if lines - missing_after[path]
    }
    still = {path: sorted(lines) for path, lines in missing_after.items() if lines}
    return CoverageDelta(newly_covered=newly, still_missing=still)


def restore_head(copy: Path, paths: Sequence[str]) -> None:
    """Put each of `paths` in the work tree `copy` back to its HEAD state, deleting new files.

    Raises `ToolExecutionError` when git cannot say whether a path is tracked or read it.
    """
    for path in paths:
        target = copy / PurePosixPath(path)
        listed = git(["ls-tree", "--name-only", "HEAD", "--", path], copy)
        if listed.returncode != 0:
            raise ToolExecutionError(f"git ls-tree failed for {path}")
        if not listed.stdout.strip():
            target.unlink(missing_ok=True)
            continue
        blob = git(["show", f"HEAD:{path}"], copy)
        if blob.returncode != 0:
            raise ToolExecutionError(f"cannot read {path} at HEAD")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.stdout)
