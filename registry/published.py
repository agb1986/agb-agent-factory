"""The publish gate: which agent versions have passed their evals, and `factory publish-check`.

A version is published once the `published` table in the trace store has a row for it. The only
writer is `publish()`, which evaluates the agent and records a row when the pass rate meets the
floor. The resolver reads the table through `published_set()` when asked for `published_only`, so a
pipeline never runs a prompt that no eval has signed off. The rules are in `docs/CONVENTIONS.md`
under "Agent address" and "CLI shape".
"""

import json
import subprocess
import sys
from collections.abc import Callable
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from factory.exit import AuthOrConfigError, ExitCode, UsageError
from runtime.trace import Trace, resolve_path

if TYPE_CHECKING:
    from registry.resolver import AgentSpec

DEFAULT_WORKERS = 4


class PublishResult(NamedTuple):
    """What `publish()` did: the eval report, whether a row was written, and the commit it names."""

    report: Any
    published: bool
    git_sha: str


def published_set(trace_path: str | PathLike[str] | None = None) -> frozenset[tuple[str, str]]:
    """Return every `(agent, version)` that has a row in the `published` table.

    `trace_path` is resolved like `Trace` does: the argument, else `FACTORY_TRACE_DB`, else
    `data/traces.duckdb`. A store that does not exist yet has nothing published, so the result is
    empty and the file is not created.
    """
    path = resolve_path(trace_path)
    if not path.exists():
        return frozenset()
    with Trace(path) as trace:
        return frozenset((row.agent, row.version) for row in trace.published())


def git_head(directory: Path | None = None) -> str:
    """Return the commit sha `HEAD` points at in `directory` (default: the working directory).

    Raises `AuthOrConfigError` when git is missing or the directory is not a repository, because a
    published row without a commit would be unreproducible.
    """
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AuthOrConfigError(
            "cannot read the git commit to record", hint="run inside a git repository with a commit"
        ) from exc
    return done.stdout.strip()


def is_dirty(directory: Path) -> bool:
    """Return whether git sees uncommitted changes under `directory`."""
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain", "--", str(directory)],
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return False
    return bool(done.stdout.strip())


def publish(
    spec: AgentSpec,
    cases: Any,
    trace: Trace,
    *,
    git_sha: str,
    workers: int = DEFAULT_WORKERS,
    runner: Callable[..., Any] | None = None,
    judge: Callable[..., Any] | None = None,
) -> PublishResult:
    """Evaluate `spec` on `cases` and, when the pass rate meets its floor, record it as published.

    Writes exactly one `published` row on a pass and nothing on a fail. `runner` and `judge` are
    passed to `evals.run.evaluate` when given, so tests can inject fakes. The eval traces are
    written to `trace` either way.
    """
    from evals.run import evaluate

    options: dict[str, Any] = {}
    if runner is not None:
        options["runner"] = runner
    if judge is not None:
        options["judge"] = judge
    report = evaluate(spec, cases, trace, workers=workers, **options)
    if report.passed:
        trace.record_published(spec.name, spec.version, report.pass_rate, git_sha)
    return PublishResult(report, report.passed, git_sha)


def command(
    ref: str,
    *,
    as_json: bool,
    workers: int = DEFAULT_WORKERS,
    runner: Callable[..., Any] | None = None,
    judge: Callable[..., Any] | None = None,
) -> int:
    """Run `factory publish-check`: evaluate the agent `ref` and publish it when it passes.

    The table goes to stderr. Under `as_json` stdout gets exactly one JSON document: the eval
    report plus `published` and `git_sha`, also on a fail. Returns 0 when published and 1 when the
    pass rate is below the floor, in which case nothing is written to `published`.
    """
    from evals.cases import CASES_FILE, load_cases
    from evals.run import resolve_ref

    if workers < 1:
        raise UsageError(f"--workers must be at least 1, not {workers}")
    spec = resolve_ref(ref)
    cases = load_cases(spec.directory / CASES_FILE)
    git_sha = git_head()
    if is_dirty(spec.directory):
        print(
            f"warning: {spec.directory} has uncommitted changes; "
            f"the recorded commit {git_sha[:12]} does not contain them",
            file=sys.stderr,
        )
    with Trace() as trace:
        result = publish(
            spec, cases, trace, git_sha=git_sha, workers=workers, runner=runner, judge=judge
        )
    print(result.report.table(), file=sys.stderr)
    if as_json:
        document = {**result.report.to_json(), "published": result.published, "git_sha": git_sha}
        print(json.dumps(document))
    return ExitCode.OK if result.published else ExitCode.ERROR
