"""Cover mode: have the test-writer add passing tests for the uncovered lines of chosen files.

The build loop calls this once it has written code; there is no `factory` command for it yet.
`resolve_cover_agent` pins the test-writer and `run_cover` runs it, all of it in a throwaway clone
of the repository (`pipelines.sandbox.checkout`):

1. The whole suite runs in the sandbox under the line tracer of `evals.coverage`, which gives the
   coverage of the target paths at the head commit. A suite that fails there ends the run with
   `CoverError`, since a new test could not be told apart from an old failure.
2. The test-writer runs in `cover` mode with the target paths and that report.
3. Its changes to files that are not tests are undone, each with a trace row tagged
   `PRODUCTION_CHANGE_REJECTED_TAG`: cover mode adds tests to the code as it is.
4. Each test file it wrote or changed runs alone in the sandbox against the head commit's code.
   A file that does not pass is put back to its head state, or deleted when it is new, with a
   trace row tagged `FAILING_TEST_REJECTED_TAG`. In cover mode a test pins down today's behaviour,
   so one that fails at head is wrong, not a find.
5. The suite runs under the tracer again. When it fails, every test change is rejected and the
   run ends with `CoverError`; otherwise the kept tests are committed in the clone, and the result
   carries the coverage delta and the patch from the head commit.

Nothing is committed to the source repository and nothing is written to disk outside the clone;
the caller decides what to do with the patch. The measure is per line, not per branch (see
`evals.coverage`).
"""

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from evals.coverage import CoverageDelta, coverage_delta, measure_coverage, restore_head
from evals.graders.sandbox_tests import PYTEST, changed_files, is_test_file
from factory.exit import FactoryError, UsageError
from pipelines.fix_loop import TEST_WRITER
from pipelines.ids import new_pipeline_run_id
from pipelines.review_panel import Runner
from pipelines.sandbox import (
    DockerSandbox,
    Sandbox,
    changed_between,
    checkout,
    commit_all,
    diff_patch,
    require_clean,
)
from registry.resolver import resolve
from registry.spec import LLMSpec
from registry.work_schemas import CoverageReport, FileAction
from runtime.budget import DEFAULT_BUDGET_USD, Budget
from runtime.runner import run
from runtime.trace import Trace, TraceRow
from tools._common import resolve_commit

FAILING_TEST_REJECTED_TAG = "cover:failing_test_rejected"
"""The `tag` of the trace row written for a test file rejected because it fails at head."""

PRODUCTION_CHANGE_REJECTED_TAG = "cover:production_change_rejected"
"""The `tag` of the trace row written for a change to a file that is not a test, undone."""

_TAIL_CHARS = 400


class CoverError(FactoryError):
    """Cover mode could not add a passing test (exit 1)."""


@dataclass(frozen=True, slots=True)
class Rejection:
    """One file the pipeline threw away, and why."""

    path: str
    tag: str
    reason: str


@dataclass(frozen=True, slots=True)
class CoverResult:
    """Everything one cover run produced. `to_json` is a plain summary for a report."""

    pipeline_run_id: str
    repo_path: str
    head: str
    commit: str
    """The commit in the clone holding the kept tests; the clone is gone once the run returns."""
    test_writer: LLMSpec
    test_writer_output: Any
    before: CoverageReport
    after: CoverageReport
    delta: CoverageDelta
    files_changed: dict[str, FileAction]
    rejected: tuple[Rejection, ...]
    patch: bytes
    total_cost_usd: float
    duration_ms: int

    def to_json(self) -> dict[str, Any]:
        """Return the result as JSON-ready data, the patch as text."""
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "repo_path": self.repo_path,
            "head": self.head,
            "test_writer": self.test_writer.address,
            "before": self.before.model_dump(mode="json"),
            "after": self.after.model_dump(mode="json"),
            "gained": self.delta.gained,
            "newly_covered": self.delta.newly_covered,
            "still_missing": self.delta.still_missing,
            "files_changed": dict(self.files_changed),
            "rejected": [
                {"path": item.path, "tag": item.tag, "reason": item.reason}
                for item in self.rejected
            ],
            "patch": self.patch.decode("utf-8", errors="replace"),
            "total_cost_usd": self.total_cost_usd,
            "duration_ms": self.duration_ms,
        }


def resolve_cover_agent(
    *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> LLMSpec:
    """Resolve the test-writer that cover mode runs, the same range the fix loop pins.

    Unless `allow_unpublished` is set, only published versions resolve. Raises `UsageError` when
    it is not an LLM agent with `sandbox: true`, and the resolver's `NotFoundError` subclasses (exit 3) when
    the agent or a matching version is missing.
    """
    name, version_range = TEST_WRITER
    spec = resolve(name, version_range, published_only=not allow_unpublished, trace_path=trace_path)
    if not isinstance(spec, LLMSpec) or spec.sandbox_profile != "offline":
        raise UsageError(
            f"{spec.address} is not an LLM agent in the offline sandbox, so cover mode cannot run it"
        )
    return spec


def run_cover(
    test_writer: LLMSpec,
    repo_path: str | Path,
    target_paths: list[str],
    trace: Trace,
    *,
    head: str = "HEAD",
    work_dir: str | Path | None = None,
    sandbox: Sandbox | None = None,
    runner: Runner = run,
    pipeline_run_id: str | None = None,
    budget: Budget | None = None,
) -> CoverResult:
    """Add passing tests for the uncovered lines of `target_paths`, from the commit `head`.

    The repository must have nothing uncommitted (`DirtyRepositoryError`, exit 2 otherwise). The
    agent works in a clone under `work_dir` (default: the system temporary directory), deleted at
    the end, and `sandbox` (default: `DockerSandbox`) runs every command. The agent's run gets
    `pipeline_run_id` and is charged to `budget` (default: `DEFAULT_BUDGET_USD`).

    Raises `UsageError` for a `work_dir` that is not a directory, the sandbox's
    `ToolUnavailableError` before the agent runs, `ToolInputError` for a target path that is not
    a Python file in the repository, `CoverError` when the suite fails at head, after the kept
    tests, or no passing test is left, and whatever the agent's run raises, `BudgetExceeded`
    among them.
    """
    start = time.monotonic()
    run_id = pipeline_run_id or new_pipeline_run_id()
    budget = budget or Budget(DEFAULT_BUDGET_USD)
    sandbox = sandbox or DockerSandbox()
    if work_dir is not None and not Path(work_dir).is_dir():
        raise UsageError(f"--work-dir {work_dir} is not a directory")
    sandbox.check()
    root = require_clean(repo_path)
    head_sha = resolve_commit(root, head)
    with checkout(root, head_sha, work_dir=work_dir) as clone:
        try:
            before = measure_coverage(clone, target_paths, run=sandbox.run)
            if before.pytest_exit_code != 0:
                raise CoverError(
                    f"the suite fails at {head_sha[:12]} with exit code "
                    f"{before.pytest_exit_code}, so new tests cannot be judged",
                    hint="fix the failing tests first",
                )
            writer_run_id = str(uuid.uuid4())
            output = runner(
                test_writer,
                test_writer.input_class.model_validate(
                    {
                        "repo_path": str(clone),
                        "mode": "cover",
                        "target_paths": [entry.path for entry in before.files],
                        "coverage": before.model_dump(mode="json"),
                    }
                ),
                trace,
                repo_path=clone,
                pipeline_run_id=run_id,
                run_id=writer_run_id,
                budget=budget,
            )
            rejected = _reject(clone, sandbox, trace, test_writer.address, run_id)
            kept = [path for path in changed_files(clone) if is_test_file(path)]
            if not any(_is_test_module(path) for path in kept):
                raise CoverError(
                    "the test-writer left no test file that passes at head",
                    hint=_reasons(rejected),
                )
            after = measure_coverage(clone, target_paths, run=sandbox.run)
            if after.pytest_exit_code != 0:
                for path in kept:
                    _record(
                        trace,
                        test_writer.address,
                        run_id,
                        Rejection(path, FAILING_TEST_REJECTED_TAG, "the whole suite fails with it"),
                    )
                raise CoverError(
                    f"the suite fails with the new tests (exit code {after.pytest_exit_code}), "
                    "so they were all rejected"
                )
            commit = commit_all(clone, "test-writer: cover " + ", ".join(target_paths))
            files = changed_between(clone, head_sha, commit)
            patch = diff_patch(clone, head_sha, commit)
        finally:
            sandbox.cleanup(clone)
    return CoverResult(
        pipeline_run_id=run_id,
        repo_path=str(root),
        head=head_sha,
        commit=commit,
        test_writer=test_writer,
        test_writer_output=output,
        before=before,
        after=after,
        delta=coverage_delta(before, after),
        files_changed=files,
        rejected=tuple(rejected),
        patch=patch,
        total_cost_usd=trace.cost_by_pipeline_run(run_id),
        duration_ms=round((time.monotonic() - start) * 1000),
    )


def _reject(
    clone: Path, sandbox: Sandbox, trace: Trace, agent: str, run_id: str
) -> list[Rejection]:
    """Undo production changes, then each test module that fails alone; return what went."""
    rejected: list[Rejection] = []
    changed = changed_files(clone)
    production = [path for path in changed if not is_test_file(path)]
    if production:
        restore_head(clone, production)
        rejected.extend(
            Rejection(path, PRODUCTION_CHANGE_REJECTED_TAG, "cover mode changes test files only")
            for path in production
        )
    for path in changed:
        if not is_test_file(path) or not _is_test_module(path) or not (clone / path).is_file():
            continue
        result = sandbox.run(clone, [*PYTEST, path])
        if result.exit_code != 0:
            restore_head(clone, [path])
            tail = " ".join(result.output.decode("utf-8", errors="replace").split())
            rejected.append(
                Rejection(
                    path,
                    FAILING_TEST_REJECTED_TAG,
                    f"fails at head with exit code {result.exit_code}: {tail[-_TAIL_CHARS:]}",
                )
            )
    _drop_bytecode(clone)
    for item in rejected:
        _record(trace, agent, run_id, item)
    return rejected


def _is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _drop_bytecode(clone: Path) -> None:
    """Delete bytecode the runs left, so the next measure imports the files as they are now."""
    for cache in clone.rglob("__pycache__"):
        if ".git" not in cache.parts:
            for item in cache.iterdir():
                item.unlink(missing_ok=True)


def _record(trace: Trace, agent: str, run_id: str, item: Rejection) -> None:
    trace.record(
        TraceRow(
            run_id=str(uuid.uuid4()),
            agent_ref=agent,
            kind="llm",
            started_at=datetime.now(UTC),
            duration_ms=0,
            input_json={"path": item.path},
            pipeline_run_id=run_id,
            cost_usd=0.0,
            decision_json={"allow": False, "path": item.path, "reason": item.reason},
            error=f"rejected {item.path}: {item.reason}",
            tag=item.tag,
        )
    )


def _reasons(rejected: list[Rejection]) -> str | None:
    return "; ".join(f"{item.path}: {item.reason}" for item in rejected)[-_TAIL_CHARS:] or None


__all__ = [
    "FAILING_TEST_REJECTED_TAG",
    "PRODUCTION_CHANGE_REJECTED_TAG",
    "CoverError",
    "CoverResult",
    "Rejection",
    "resolve_cover_agent",
    "run_cover",
]
