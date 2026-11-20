"""The `sandbox_tests` grader: run the tests in the sandbox and see what the agent really did.

An agent that writes tests or fixes code is graded on the work tree it left behind, not on what it
says. `check_run` runs pytest in the offline sandbox (`tools.sandbox.run_in_sandbox`) over that
tree and compares the outcome with a case's `expected.run`: the new tests fail or pass, they pass
once the fixture is put back to its `base` state, the whole suite passes, production code and
test files changed or did not, the tests add line coverage (`evals.coverage`), and the public names
and signatures of chosen files are the same as at HEAD (`evals.public_api`). It returns the
failures as strings and never calls a model; the caller (`evals.graders.grade`) merges them with
the other deterministic checks.

The checks run commands, so they need Docker and the image `tools.sandbox.SANDBOX_IMAGE`; without
them `tools._common.ToolUnavailableError` propagates and ends the eval. They read the repository
but never write to it: the base-state run works on a temporary copy.
"""

import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from evals.cases import ExpectedCoverage, ExpectedRun
from evals.coverage import CoverageError, coverage_delta, measure_coverage, restore_head
from evals.public_api import check_public_api
from fixtures.build import BASE_REF
from tools._common import (
    ToolError,
    ToolExecutionError,
    ToolInputError,
    git,
    resolve_inside,
    resolve_root,
)
from tools.sandbox import run_in_sandbox

PYTEST = ["python", "-m", "pytest", "-q"]
"""The command every check runs, the same one the agents are told to use."""

PYTEST_FAILED = 1
"""Pytest's exit code for "ran, and some tests failed". A collection error is 2 and is not it."""

_NOISE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
_TAIL_CHARS = 400


class _Stop(Exception):
    """A check cannot go on; the message is the failure to report."""


def is_test_file(path: str) -> bool:
    """Return whether `path` is a test file: under a `tests` directory, or named like one.

    A name counts when it is `conftest.py`, starts with `test_` or ends with `_test.py`. Every other
    path is production code, including docs and configuration.
    """
    pure = PurePosixPath(path)
    name = pure.name
    return (
        "tests" in pure.parts[:-1]
        or name == "conftest.py"
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


def changed_files(repo_path: str | Path) -> list[str]:
    """Return the paths that differ between the work tree and HEAD, new files included, sorted.

    Caches the tools leave behind (`__pycache__`, `.pytest_cache`) are not counted. A rename lists
    both its old and new path. Raises `ToolExecutionError` when git cannot report the status.
    """
    root = resolve_root(repo_path)
    proc = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    if proc.returncode != 0:
        raise ToolExecutionError("git status failed in the repository under test")
    entries = proc.stdout.decode("utf-8", errors="replace").split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" and index < len(entries):  # the next entry is the old path
            paths.add(entries[index])
            index += 1
    return sorted(path for path in paths if not _is_noise(path))


def check_run(expected: ExpectedRun, actual: Mapping[str, Any], repo_path: str | Path) -> list[str]:
    """Return why the work tree at `repo_path` does not meet `expected`; empty when it does.

    `actual` is the agent's output as a dict; `new_tests` reads its `test_files` list of paths. The
    cheap checks on what changed run first, then the commands in the sandbox. Raises
    `ToolUnavailableError` when Docker or the image is missing.
    """
    root = resolve_root(repo_path)
    failures: list[str] = []
    failures.extend(_check_changes(expected, root))
    if expected.public_api is not None:
        failures.extend(check_public_api(root, expected.public_api))
    if expected.new_tests is not None:
        try:
            files = _test_files(root, actual.get("test_files"))
        except _Stop as stop:
            failures.append(str(stop))
        else:
            failures.extend(_check_new_tests(expected, root, files))
    if expected.suite is not None:
        result = run_in_sandbox(root, PYTEST)
        if result.exit_code != 0:
            failures.append(
                f"the full suite fails (exit code {result.exit_code}): {_tail(result.output)}"
            )
    if expected.coverage is not None:
        failures.extend(_check_coverage(expected.coverage, root))
    return failures


def _check_coverage(expected: ExpectedCoverage, root: Path) -> list[str]:
    """Compare the coverage of `expected.paths` before and after the agent's changes."""
    try:
        after = measure_coverage(root, expected.paths)
        with tempfile.TemporaryDirectory(prefix="factory-eval-cover-") as scratch:
            copy = Path(scratch) / "repo"
            shutil.copytree(root, copy, symlinks=True)
            restore_head(copy, changed_files(copy))
            before = measure_coverage(copy, expected.paths)
    except ToolInputError as exc:
        return [f"coverage: {exc.message}"]
    except (CoverageError, ToolExecutionError) as exc:
        return [f"coverage could not be measured: {exc.message}"]
    delta = coverage_delta(before, after)
    failures: list[str] = []
    if delta.gained < expected.min_gain:
        failures.append(
            f"coverage gained {delta.gained} lines, expected at least {expected.min_gain}"
        )
    if expected.max_missing is not None and delta.missing > expected.max_missing:
        failures.append(
            f"coverage still misses {delta.missing} lines, expected at most "
            f"{expected.max_missing}: {_missing_text(delta.still_missing)}"
        )
    return failures


def _missing_text(missing: Mapping[str, list[int]]) -> str:
    return "; ".join(f"{path} {', '.join(map(str, lines))}" for path, lines in missing.items())


def _check_changes(expected: ExpectedRun, root: Path) -> list[str]:
    if expected.production is None and expected.tests is None:
        return []
    try:
        changed = changed_files(root)
    except ToolError as exc:
        return [exc.message]
    production = [path for path in changed if not is_test_file(path)]
    tests = [path for path in changed if is_test_file(path)]
    failures: list[str] = []
    for want, name, paths in (
        (expected.production, "production code", production),
        (expected.tests, "test files", tests),
    ):
        if want == "unchanged" and paths:
            failures.append(f"{name} changed, expected none: {', '.join(paths)}")
        if want == "changed" and not paths:
            failures.append(f"{name} unchanged, expected a change")
    return failures


def _test_files(root: Path, listed: object) -> list[str]:
    if not isinstance(listed, list) or not listed:
        raise _Stop("test_files is empty: the agent named no test file to run")
    files: list[str] = []
    for item in listed:
        try:
            rel = resolve_inside(root, item)
        except ToolError as exc:
            raise _Stop(f"test_files: {exc.message}") from exc
        if not (root / rel).is_file():
            raise _Stop(f"test_files: {item!r} is not a file in the repository")
        files.append(rel.as_posix())
    return files


def _check_new_tests(expected: ExpectedRun, root: Path, files: list[str]) -> list[str]:
    failures: list[str] = []
    result = run_in_sandbox(root, [*PYTEST, *files])
    if expected.new_tests == "fail" and result.exit_code != PYTEST_FAILED:
        failures.append(
            f"new tests should fail with exit code {PYTEST_FAILED}, got {result.exit_code}: "
            f"{_tail(result.output)}"
        )
    if expected.new_tests == "pass" and result.exit_code != 0:
        failures.append(
            f"new tests should pass, got exit code {result.exit_code}: {_tail(result.output)}"
        )
    if expected.passes_on_base:
        failures.extend(_check_on_base(root, files))
    return failures


def _check_on_base(root: Path, files: list[str]) -> list[str]:
    """Run `files` on a copy of `root` whose non-test files are back at the `base` ref."""
    with tempfile.TemporaryDirectory(prefix="factory-eval-base-") as scratch:
        copy = Path(scratch) / "repo"
        shutil.copytree(root, copy, symlinks=True)
        try:
            _restore_base(copy)
        except _Stop as stop:
            return [str(stop)]
        result = run_in_sandbox(copy, [*PYTEST, *files])
    if result.exit_code != 0:
        message = (
            f"new tests should pass on the {BASE_REF} state, got exit code "
            f"{result.exit_code}: {_tail(result.output)}"
        )
        return [message]
    return []


def _restore_base(copy: Path) -> None:
    proc = git(["diff", "--name-status", "-z", "--no-renames", BASE_REF, "HEAD"], copy)
    if proc.returncode != 0:
        raise _Stop(f"no {BASE_REF} ref to compare with in the repository under test")
    items = proc.stdout.decode("utf-8", errors="replace").split("\0")
    for status, path in zip(items[0::2], items[1::2], strict=False):
        if is_test_file(path):
            continue
        target = copy / path
        if status == "A":
            target.unlink(missing_ok=True)
            continue
        blob = git(["show", f"{BASE_REF}:{path}"], copy)
        if blob.returncode != 0:
            raise _Stop(f"cannot read {path} at {BASE_REF}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.stdout)


def _is_noise(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in _NOISE_DIRS for part in parts) or path.endswith((".pyc", ".pyo"))


def _tail(output: bytes) -> str:
    text = " ".join(output.decode("utf-8", errors="replace").split())
    return text if len(text) <= _TAIL_CHARS else f"...{text[-_TAIL_CHARS:]}"
