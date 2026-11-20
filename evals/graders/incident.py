"""The `incident` grader: check a triage-diagnostics answer against the crash planted in a fixture.

A case names the planted fault's file, the exception the hand-written event shows and the file
that raised it (`evals.cases.ExpectedIncident`). `check_incident` passes an answer when its
`suspected_paths` list the fault's file and its `reproduction_test` reproduces the event: written
into a copy of the fixture at `head`, which plays the release tag, and run in the offline sandbox,
the test must fail (pytest exit code 1) with that exception, raised from that file. A test that
fails on an assertion, on an import error or for any other reason does not reproduce the event,
and neither does a test that passes.

`reproduces` is that rule on its own, and `pipelines.incident` applies it to the test
test-writer commits, so the eval and the pipeline count a reproduction the same way. The check
runs commands, so it needs Docker and the sandbox image; without them
`tools._common.ToolUnavailableError` propagates and ends the eval. It never writes to the fixture.
"""

import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evals.cases import ExpectedIncident
from evals.graders.sandbox_tests import PYTEST, PYTEST_FAILED
from tools._common import ToolError, resolve_inside, resolve_root
from tools.sandbox import run_in_sandbox

REPRODUCE_ARGS = ("--tb=short", "-p", "no:cacheprovider")
"""What the reproduction run adds to `PYTEST`: short tracebacks that name each frame's file."""

_TAIL_CHARS = 400


def reproduces(exit_code: int, output: str, exception: str, crash_file: str) -> bool:
    """Return whether a pytest run reproduced an event: `exception` raised from `crash_file`.

    The run must have failed its tests (exit code `PYTEST_FAILED`, so not a collection error),
    pytest must report an error line `E   <exception>` (the class may carry a module prefix,
    such as `json.decoder.JSONDecodeError`), and a traceback line must point into `crash_file`,
    a path relative to the repository root. `exception` may be dotted; only its last part is
    compared.
    """
    if exit_code != PYTEST_FAILED:
        return False
    name = re.escape(exception.rsplit(".", 1)[-1])
    raised = re.compile(rf"^E\s+(?:[\w.]+\.)?{name}\b", re.MULTILINE)
    at_file = re.compile(rf"(?:^|[\s/]){re.escape(crash_file)}:\d+", re.MULTILINE)
    return bool(raised.search(output)) and bool(at_file.search(output))


def check_incident(
    expected: ExpectedIncident, actual: Mapping[str, Any], repo_path: str | Path
) -> list[str]:
    """Check `actual`, a triage-diagnostics output, against `expected`; return the failures.

    `repo_path` is the fixture repository at `head`. The test is written into a temporary copy of
    it, so the fixture is never changed; a test path that escapes the copy or names a file that
    already exists is a failure, not an error.
    """
    failures: list[str] = []
    paths = actual.get("suspected_paths") or []
    if expected.suspected_path not in paths:
        failures.append(f"suspected_paths {paths!r} do not include {expected.suspected_path}")
    test = actual.get("reproduction_test") or {}
    path, code = test.get("path"), test.get("code")
    if not isinstance(path, str) or not isinstance(code, str) or not code.strip():
        failures.append("reproduction_test has no path and code to run")
        return failures
    problem = _run_test(expected, Path(repo_path), path, code)
    if problem is not None:
        failures.append(problem)
    return failures


def _run_test(expected: ExpectedIncident, repo: Path, path: str, code: str) -> str | None:
    with tempfile.TemporaryDirectory(prefix="factory-eval-incident-") as scratch:
        copy = Path(scratch) / "repo"
        shutil.copytree(repo, copy, symlinks=True)
        root = resolve_root(copy)
        try:
            relative = resolve_inside(root, path)
        except ToolError as exc:
            return f"reproduction_test path {path!r} is not usable: {exc}"
        target = root / relative
        if target.exists():
            return f"reproduction_test path {path} already exists at the release"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        result = run_in_sandbox(root, [*PYTEST, *REPRODUCE_ARGS, relative.as_posix()])
    output = result.output.decode("utf-8", errors="replace")
    if reproduces(result.exit_code, output, expected.exception, expected.crash_file):
        return None
    return (
        f"reproduction_test does not fail with {expected.exception} raised from "
        f"{expected.crash_file} (exit code {result.exit_code}): {_tail(output)}"
    )


def _tail(output: str) -> str:
    text = " ".join(output.split())
    return text if len(text) <= _TAIL_CHARS else f"...{text[-_TAIL_CHARS:]}"
