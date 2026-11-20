"""The build loop: take one `ready` work item to a reviewed pull request.

`resolve_build_agents` pins the agents before anything runs, `run_build` runs them under one
pipeline run id and `command` is what `factory build` calls. The loop belongs to the dev team (the
coder) and the qa team (the review panel); it decides nothing a model should, and no model moves the
work item. The flow (docs/build-loop.md has the options and outputs):

1. Preflight, with the work item untouched: the item must be `ready` and of a kind with a coder
   (`CODERS`), the repository clean, its `origin` on github.com (ADR 0003) and, when the ticket
   names a repository, the same one. Any failure here raises and writes nothing.
2. The pipeline run is linked to the item and, unless it is a dry run, the item moves to
   `in_progress` (ADR 0002), before the clone is made.
3. In a throwaway clone (`pipelines.sandbox.checkout`), the coder builds the ticket. Its manifest's
   `files` policy is enforced on the host twice: `pipelines.file_policy.FilePolicyGuard` refuses a
   denied `fs.write_file`, and `check_changed` refuses a denied path changed by another route. A
   refusal writes a trace row tagged `FILE_WRITE_REFUSED_TAG` and ends the run with
   `FileWriteRefusedError`. A coder answer with `blocked` set ends it with `CoderBlockedError`.
4. The coder's work is committed in the clone, the clone is cleaned and the whole suite runs in the
   sandbox; a failure ends the run with `SuiteFailedError`. A kind with a check in `KIND_CHECKS`
   adds its own: for `refactor`, `pipelines.behaviour.BehaviourCheck` probes the suite before the
   coder's first round and after each passing suite, and refuses a change to a test, to a test's
   state or to coverage with its own `BehaviourError`. For `docs`, `pipelines.docs_check.DocsCheck`
   refuses a round that changed source code outside its comments and docstrings with its own
   `DocsCheckError`.
5. The review panel reviews the change from the ticket's `ref` to the coder's commit. When it asks
   for changes the coder runs once more, with the merged findings appended to the ticket, and steps
   3 to 5 repeat. A second request for changes ends the run with `ChangesRequestedError`.
6. When the panel ships, the commit is fetched into the source repository's object store (no ref is
   made) and `runtime.outside.write("github.create_pr", ...)` pushes the branch
   `factory/<pipeline_run_id>` and opens the pull request, for the judge manifest and with the
   judge's row as the parent. The item moves to `in_review`. The source repository gains no branch
   and no tag, though git may record the remote-tracking ref
   `refs/remotes/origin/factory/<pipeline_run_id>` for the push, depending on the git version.

Every other end is settled in a `finally` block, with `<ErrorClass>: <message>` and the report's
path as the reason. A stop a person can answer (`stop_trigger`, the table in docs/CONVENTIONS.md,
Escalations) writes an escalation record and moves the item to `escalated` in one step
(`runtime.escalations.escalate`); any other stop or crash moves it to `blocked`. A run after an
answer gets the answers in its ticket body (`runtime.escalations.ticket_for_run`).

A dry run (`dry_run=True`) runs every step, reads included, but makes no outside write and moves no item; it still links the run,
so the item's cost counts it. The report carries the planned pull request with a null `url`, and
the change it would have opened is written as `<pipeline run id>.patch`, which `git apply` takes at
the ticket's `ref`, next to the report; the pull request's `patch` names it. The report,
`<pipeline run id>.json`, is written last, however the run ended after preflight.
"""

import dataclasses
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from evals.graders.sandbox_tests import PYTEST
from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from pipelines.behaviour import (
    BehaviourChangedError,
    BehaviourCheck,
    CoverageDroppedError,
    CoverageUnmeasuredError,
    KindCheck,
    TestFileChangedError,
)
from pipelines.docs_check import (
    CodeChangedError,
    DocsCheck,
    SourceFileActionError,
    UncheckedFileError,
    UnparsableSourceError,
)
from pipelines.file_policy import GUARDED_TOOL, FilePolicy, FilePolicyGuard, check_changed
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
from registry.common_schemas import Ticket
from registry.resolver import resolve
from registry.spec import FilesSpec, LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget, BudgetExceeded
from runtime.escalations import AgentOutput, Trigger, escalate, ticket_for_run
from runtime.jev_runner import run_jev
from runtime.outside import OutsideContext, write
from runtime.runner import API_KEY_ENV, run
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItem, WorkItems
from tools._common import fail_hint, git, resolve_commit, resolve_root
from tools.github import branch_for
from tools.sandbox import SandboxResult

FEATURE_CODER: tuple[str, str] = ("feature-coder", "^0.1")
"""The agent that builds a ticket and the version range it is pinned to."""

CODERS: Mapping[str, tuple[str, str]] = {
    "feature": FEATURE_CODER,
    "bug": FEATURE_CODER,
    "refactor": ("refactorer", "^0.1"),
    "docs": ("documentation-agent", "^0.1"),
}
"""The coder for each ticket kind the build loop takes (docs/CONVENTIONS.md, Work items)."""

KIND_CHECKS: Mapping[str, Callable[[], KindCheck]] = {
    "refactor": BehaviourCheck,
    "docs": DocsCheck,
}
"""The extra checks a ticket kind's rounds go through, made once per run (`pipelines.behaviour`)."""

OTHER_KINDS: Mapping[str, str] = {
    "incident": "an incident goes to `factory hotfix`, which fixes it against its reproduction test",
    "research": "research has no pipeline: run the research agent with `factory run`",
}
"""Where each ticket kind the build loop refuses goes instead."""

GH_TOKEN_ENV = "GH_TOKEN"
"""The token `tools.github` reads; a dry run needs it too, because the target is read."""

ACTOR = "pipeline:build_loop"
"""The actor of every move the build loop makes on a work item."""

FILE_WRITE_REFUSED_TAG = "build:file_write_refused"
"""The `tag` of the trace row written when the coder's change to a denied path is refused."""

DEFAULT_OUT_DIR = Path("data/builds")
"""Where the report goes, relative to the working directory like the trace database."""

MAX_ROUNDS = 2
"""How many times the coder runs: once, and once more when the panel asks for changes."""

SUITE_TAIL_CHARS = 2000
"""How much of the end of the suite's output the report keeps."""


class BuildError(FactoryError):
    """The build loop could not produce a change worth opening a pull request for (exit 1)."""


class FileWriteRefusedError(BuildError):
    """The coder changed, or tried to change, a path its manifest's `files` policy denies."""


class CoderBlockedError(BuildError):
    """The coder answered `blocked`: it cannot build the ticket as written."""


class SuiteFailedError(BuildError):
    """The test suite did not pass in the sandbox after the coder was done."""


class ChangesRequestedError(BuildError):
    """The panel asked for changes twice, so the change does not ship."""


class NoChangeError(BuildError):
    """The coder changed nothing: the code at the ticket's ref may already do what it asks."""


ESCALATE_ON: tuple[tuple[type[FactoryError], Trigger], ...] = (
    (CoderBlockedError, "blocked_output"),
    (BudgetExceeded, "budget"),
    (ChangesRequestedError, "changes_requested"),
    (NoChangeError, "contradiction"),
    (SuiteFailedError, "check_failed"),
    (FileWriteRefusedError, "check_failed"),
    (TestFileChangedError, "check_failed"),
    (BehaviourChangedError, "check_failed"),
    (CoverageDroppedError, "check_failed"),
    (CoverageUnmeasuredError, "check_failed"),
    (CodeChangedError, "check_failed"),
    (UnparsableSourceError, "check_failed"),
    (SourceFileActionError, "check_failed"),
    (UncheckedFileError, "check_failed"),
)
"""The stops a person can answer and the escalation trigger of each; every other stop is `blocked`.

docs/CONVENTIONS.md (Escalations) has the full table, with the reason for each row.
"""


def stop_trigger(
    exc: BaseException,
    table: Sequence[tuple[type[FactoryError], Trigger]] = ESCALATE_ON,
) -> Trigger | None:
    """Return the escalation trigger for a stop on `exc`, or None when the item goes to `blocked`.

    The first row of `table` (default `ESCALATE_ON`) whose class `exc` is an instance of wins, so a
    subclass of a listed error escalates the same way. The hotfix pipeline passes its own table.
    """
    for cls, trigger in table:
        if isinstance(exc, cls):
            return trigger
    return None


def escalation_question(
    exc: FactoryError, trigger: Trigger, rounds: Sequence[Mapping[str, Any]]
) -> str:
    """Return the question an escalation for a stop on `exc` asks a person.

    It names what stopped the run and what a person can decide. For `blocked_output` it adds the
    coder's `blocked_reason` and `open_questions` from the last round.
    """
    coder = (rounds[-1].get("coder") or {}) if rounds else {}
    output = coder.get("output") or {}
    lines = [f"{exc.message}."]
    if trigger == "blocked_output":
        lines = [f"The coder cannot build the ticket as written: {exc.hint or 'no reason given'}."]
        lines += [f"- {q}" for q in output.get("open_questions") or [] if str(q).strip()]
        lines.append("What should it do?")
    elif trigger == "budget":
        lines.append("Should the next run get a bigger budget, or should the ticket be cut down?")
    elif trigger == "changes_requested":
        lines.append(
            "The panel's findings are in the outputs below. Which of them should the next run "
            "address, and how?"
        )
    elif trigger == "contradiction":
        lines.append(
            "The ticket asks for a change, but the coder made none, so the code at the ticket's "
            "ref may already do what it asks. Is the ticket still needed, and what is missing?"
        )
    else:
        if exc.hint:
            lines.append(exc.hint)
        lines.append("How should the next run get past this check?")
    return "\n\n".join(lines)


@dataclass(frozen=True, slots=True)
class BuildAgents:
    """The resolved agents of one build run: the coder for the ticket's kind and the panel."""

    coder: LLMSpec
    panel: Panel


def coder_for(kind: str) -> tuple[str, str]:
    """Return the `(name, version range)` of the coder for a ticket `kind`.

    Raises `UsageError` (exit 2) for a kind the build loop does not take, with a hint naming where
    it goes instead.
    """
    if kind in CODERS:
        return CODERS[kind]
    raise UsageError(
        f"the build loop does not take {kind} tickets",
        hint=OTHER_KINDS.get(kind, f"kinds it takes: {', '.join(CODERS)}"),
    )


def check_buildable(item: WorkItem) -> None:
    """Raise `UsageError` (exit 2) unless `item` is `ready` and of a kind the build loop takes."""
    if item.status != "ready":
        raise UsageError(
            f"{item.id} is {item.status}, and the build loop takes only ready items",
            hint="see what is ready with `factory ticket ready`",
        )
    coder_for(item.kind)


def resolve_build_agents(
    kind: str, *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> BuildAgents:
    """Resolve the coder for a ticket `kind` and the full review panel with triage and judge gate.

    Unless `allow_unpublished` is set, only published versions resolve. Raises `UsageError` for a
    kind the build loop does not take, and when the coder is not an LLM agent sandboxed offline or
    has no action guard or no `files` policy; the resolver's `NotFoundError` subclasses (exit 3)
    when an agent or version is missing.
    """
    name, version_range = coder_for(kind)
    spec = resolve(name, version_range, published_only=not allow_unpublished, trace_path=trace_path)
    if not isinstance(spec, LLMSpec):
        raise UsageError(f"{spec.address} is not an LLM agent, so the build loop cannot run it")
    if spec.sandbox_profile != "offline":
        raise UsageError(
            f"{spec.address} is not sandboxed offline, so it cannot work on the repository",
            hint="set `sandbox: true` in its manifest",
        )
    if spec.guard is None:
        raise UsageError(
            f"{spec.address} has no action guard, so the build loop will not run it",
            hint="set `guard` in its manifest",
        )
    _coder_files(spec)
    panel = resolve_panel(allow_unpublished=allow_unpublished, trace_path=trace_path)
    return BuildAgents(coder=spec, panel=panel)


def _coder_files(coder: LLMSpec) -> FilesSpec:
    if coder.files is None:
        raise UsageError(
            f"{coder.address} has no files policy, so the build loop will not run it",
            hint="set `files.deny` in its manifest to the paths the coder may not change",
        )
    return coder.files


def _refuse(
    trace: Trace, pipeline_run_id: str, agent: str, path: str, reason: str, check: str
) -> FileWriteRefusedError:
    """Write the refusal row for the coder's change to `path` and return the error to raise."""
    message = f"the coder may not change {path}: {reason}"
    trace.record(
        TraceRow(
            run_id=str(uuid.uuid4()),
            agent_ref=agent,
            kind="llm",
            started_at=datetime.now(UTC),
            duration_ms=0,
            input_json={"tool": GUARDED_TOOL, "path": path},
            pipeline_run_id=pipeline_run_id,
            cost_usd=0.0,
            decision_json={"allow": False, "path": path, "reason": reason, "check": check},
            error=message,
            tag=FILE_WRITE_REFUSED_TAG,
        )
    )
    return FileWriteRefusedError(
        message, hint="the coder's `files` policy keeps how the suite runs out of its reach"
    )


@dataclass(frozen=True, slots=True)
class BuildResult:
    """What one build run produced: the report `factory build` writes and prints, and its code."""

    document: dict[str, Any]
    code: ExitCode

    def to_json(self) -> dict[str, Any]:
        """Return the report, as written to `report_path`."""
        return self.document

    def text(self) -> str:
        """Return a short plain-text report of the run, for use without `--json`."""
        doc = self.document
        item = doc["work_item"]
        lines = [f"build run {doc['pipeline_run_id']}: {item['id']} {item['ticket']['title']}"]
        for r in doc["rounds"]:
            parts = [f"round {r['number']}: {len(r['files_changed'])} files changed"]
            if r["suite"] is not None:
                state = "passed" if r["suite"]["passed"] else "failed"
                parts.append(f"suite {state} in {r['suite']['seconds']:.1f}s")
            if r.get("behaviour") is not None:
                moved = len(r["behaviour"]["tests_changed"])
                parts.append(
                    f"behaviour: {moved} tests changed state" if moved else "behaviour held"
                )
            if r["panel"] is not None:
                parts.append("panel ships" if r["panel"]["ship"] else "panel asks for changes")
            lines.append("  " + "; ".join(parts))
        pull = doc["pull_request"]
        if pull is not None and pull["url"] is not None:
            lines.append(f"pull request {pull['url']}")
        elif pull is not None:
            lines.append(f"dry run: would open a pull request from {pull['branch']}")
            if pull.get("patch") is not None:
                lines.append(f"patch {pull['patch']}")
        if doc["stopped"] is not None:
            lines.append(f"stopped: {doc['stopped']['message']}")
            if doc["stopped"]["hint"]:
                lines.append(f"  hint: {doc['stopped']['hint']}")
        lines.append(f"work item {item['id']}: {item['status_before']} -> {item['status_after']}")
        if doc["board_error"] is not None:
            lines.append(f"  could not move the work item: {doc['board_error']['message']}")
        lines.append(f"report {doc['report_path']}")
        lines.append(f"cost ${doc['total_cost_usd']:.4f}")
        return "\n".join(lines)


def _tail(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")[-SUITE_TAIL_CHARS:]


def _suite_json(suite: SandboxResult) -> dict[str, Any]:
    return {
        "argv": list(suite.argv),
        "exit_code": suite.exit_code,
        "passed": suite.exit_code == 0,
        "seconds": round(suite.seconds, 3),
        "output_tail": _tail(suite.output),
    }


def _stop_json(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, FactoryError):
        return {
            "type": type(exc).__name__,
            "message": exc.message,
            "hint": exc.hint,
            "exit_code": int(exc.code),
        }
    return {
        "type": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
        "hint": None,
        "exit_code": int(ExitCode.ERROR),
    }


def _findings(panel: PanelResult) -> list[dict[str, Any]]:
    """Return the panel's findings: the judge's merged ones, or every reviewer's without a judge."""
    if panel.judgement is not None:
        return list(panel.judgement.model_dump(mode="json").get("merged_findings", []))
    return [
        finding.model_dump(mode="json")
        for reviewer in panel.reviewers
        if reviewer.review is not None
        for finding in reviewer.review.findings
    ]


def findings_ticket(ticket: Ticket, findings: Sequence[Mapping[str, Any]]) -> Ticket:
    """Return `ticket` with the panel's `findings` appended to its body, for the coder's next round.

    Each finding becomes one entry: severity, place, title, body and the suggested fix when there
    is one. Everything else in the ticket is unchanged.
    """
    lines = ["## Review findings to address", ""]
    for f in findings:
        where = f"{f.get('file')}:{f.get('line_start')}-{f.get('line_end')}"
        lines.append(f"- [{f.get('severity')}] {where} {f.get('title')}: {f.get('body')}")
        if f.get("suggested_fix"):
            lines.append(f"  Suggested fix: {f['suggested_fix']}")
    if not findings:
        lines.append("- The panel asked for changes without a finding; see its scores.")
    body = f"{ticket.body.rstrip()}\n\n" + "\n".join(lines)
    return ticket.model_copy(update={"body": body})


def pr_body(item: WorkItem, ticket: Ticket, coder_output: BaseModel) -> str:
    """Return the pull request body: the ticket, its acceptance, what the coder did, the item.

    `github.create_pr` adds the pipeline run and its marker below it.
    """
    out = coder_output.model_dump(mode="json")
    lines = [ticket.body.rstrip(), "", "## Acceptance", ""]
    lines.extend(f"- {criterion}" for criterion in ticket.acceptance)
    lines += ["", "## What the coder did", "", str(out.get("summary", "")).strip()]
    files = out.get("files_changed") or []
    if files:
        lines.append("")
        lines.extend(f"- {f.get('action')} `{f.get('path')}`: {f.get('summary')}" for f in files)
    lines += ["", f"Work item `{item.id}`."]
    return "\n".join(lines).strip() + "\n"


def _judge_run_id(trace: Trace, panel: PanelResult) -> str | None:
    """Return the `run_id` of the judge's last row in this pipeline run, or None without one."""
    if panel.judgement is None:
        return None
    found = None
    for row in trace.pipeline_runs(panel.pipeline_run_id):
        if row.agent_ref == panel.judge.address and row.kind == "llm" and row.tag is None:
            found = row.run_id
    return found


def _fetch_into(root: Path, clone: Path, sha: str) -> None:
    """Copy commit `sha` and its history from `clone` into `root`'s object store, making no ref.

    `github.create_pr` pushes from the source repository, never from the clone, whose `.git` the
    agent could have changed. Raises `BuildError` when git cannot fetch it.
    """
    proc = git(
        [
            "-c",
            f"core.hooksPath={os.devnull}",
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            "--no-tags",
            "--",
            str(clone),
            sha,
        ],
        root,
    )
    if proc.returncode != 0:
        raise BuildError(
            f"cannot copy commit {sha[:12]} into {root}", hint=fail_hint(proc) or "git fetch failed"
        )


def _pick_repo(ticket: Ticket, repo_path: str | Path | None) -> Path:
    """Return the repository to build in: `repo_path`, else the ticket's path, else `.`."""
    named = ticket.repo if ticket.repo.startswith("/") else None
    if repo_path is None:
        return require_clean(named or ".")
    root = require_clean(repo_path)
    if named is not None and resolve_root(named) != root:
        raise UsageError(
            f"--repo {repo_path} is not the ticket's repository {named}",
            hint="drop --repo, or fix the ticket's repo",
        )
    return root


def run_build(
    agents: BuildAgents,
    item_id: str,
    trace: Trace,
    *,
    repo_path: str | Path | None = None,
    dry_run: bool = False,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    work_dir: str | Path | None = None,
    sandbox: Sandbox | None = None,
    runner: Runner = run,
    jev_runner: JevRunner = run_jev,
    pipeline_run_id: str | None = None,
    budget: Budget | None = None,
) -> BuildResult:
    """Build the `ready` work item `item_id` and, when the panel ships it, open its pull request.

    The repository is `repo_path`, else the ticket's `repo` when it is a path, else the working
    directory; the build starts from the ticket's `ref`. Raises, before the item is touched,
    `WorkItemNotFound` (exit 3), and `UsageError` (exit 2) for an item that is not `ready`, a kind
    the loop does not take, a dirty repository, an `origin` that is not on github.com or is not
    the ticket's repository, a `repo_path` that is not the ticket's, or a bad `work_dir` or
    `out_dir`; the sandbox's `ToolUnavailableError` (exit 4) before any agent runs.

    After that nothing is raised for a `FactoryError`: the run stops and the result carries the
    stop and its exit code. Unless it is a dry run, a stop `stop_trigger` names writes an
    escalation record and moves the item to `escalated`; any other end moves it to `blocked`, and
    so does a stop whose escalation cannot be written. The report's `escalation` is the record's
    id, or null. The coder gets the ticket with the answers to earlier escalations appended. Any
    other exception is recorded the same way and then raised. The report `<pipeline run id>.json` goes to `out_dir`
    on every end after preflight. Every agent call and the outside write share `pipeline_run_id`
    and are charged to `budget` (default: `DEFAULT_BUDGET_USD`). The result's code is 0 when the
    pull request was opened, or would have been in a dry run.
    """
    start = time.monotonic()
    run_id = pipeline_run_id or new_pipeline_run_id()
    budget = budget or Budget(DEFAULT_BUDGET_USD)
    sandbox = sandbox or DockerSandbox()
    items = WorkItems(trace)
    item = items.get(item_id)
    check_buildable(item)
    ticket = item.ticket()
    root = _pick_repo(ticket, repo_path)
    head_sha = resolve_commit(root, ticket.ref)
    context = OutsideContext.for_repository(
        root, pipeline_run_id=run_id, trace=trace, dry_run=dry_run
    )
    if not ticket.repo.startswith("/") and not context.target.same(ticket.repo):
        raise UsageError(
            f"origin is {context.target.full_name}, the ticket's repository is {ticket.repo}",
            hint="build in a clone of the ticket's repository",
        )
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

    report_path = out / f"{run_id}.json"
    depends_on = [f"{dep}: {items.get(dep).title}" for dep in item.depends_on]
    rounds: list[dict[str, Any]] = []
    pull: dict[str, Any] | None = None
    stopped: dict[str, Any] | None = None
    board_error: dict[str, Any] | None = None
    escalation: str | None = None
    stop_exc: FactoryError | None = None
    workspace: dict[str, str] = {}
    shipped = False
    try:
        items.link_run(item.id, run_id)
        if not dry_run:
            items.move(item.id, "in_progress", "build started", actor=ACTOR, pipeline_run_id=run_id)
        pull = _build(
            agents,
            item,
            ticket_for_run(trace, item),
            depends_on,
            trace,
            root=root,
            head_sha=head_sha,
            context=context,
            work_dir=work_dir,
            sandbox=sandbox,
            runner=runner,
            jev_runner=jev_runner,
            run_id=run_id,
            budget=budget,
            rounds=rounds,
            workspace=workspace,
        )
        if dry_run:
            patch_path = out / f"{run_id}.patch"
            patch_path.write_bytes(diff_patch(root, head_sha, pull["commit"]))
            pull["patch"] = str(patch_path)
        else:
            items.move(
                item.id,
                "in_review",
                f"pull request opened: {pull['url']}",
                actor=ACTOR,
                pipeline_run_id=run_id,
            )
        shipped = True
    except BaseException as exc:
        stopped = _stop_json(exc)
        if not isinstance(exc, FactoryError):
            raise
        stop_exc = exc
    finally:
        if not dry_run and not shipped:
            cause = (
                f"{stopped['type']}: {stopped['message']}"
                if stopped is not None
                else "the build did not finish"
            )
            reason = f"{cause}; report {report_path}"
            trigger = stop_trigger(stop_exc) if stop_exc is not None else None
            if stop_exc is not None and trigger is not None:
                try:
                    escalation = escalate(
                        trace,
                        item.id,
                        trigger=trigger,
                        question=escalation_question(stop_exc, trigger, rounds),
                        reason=reason,
                        actor=ACTOR,
                        pipeline_run_id=run_id,
                        diff=workspace.get("diff", ""),
                        agent_outputs=_last_outputs(agents, rounds),
                        suite_tail=_last_suite_tail(rounds),
                    ).id
                except FactoryError as exc:
                    board_error = _stop_json(exc)
            if escalation is None:
                try:
                    items.move(item.id, "blocked", reason, actor=ACTOR, pipeline_run_id=run_id)
                except FactoryError as exc:
                    board_error = board_error or _stop_json(exc)
        document = {
            "pipeline_run_id": run_id,
            "work_item": {
                "id": item.id,
                "status_before": item.status,
                "status_after": items.get(item.id).status,
                "ticket": ticket.model_dump(mode="json"),
            },
            "repo_path": str(root),
            "head": head_sha,
            "dry_run": dry_run,
            "coder": agents.coder.address,
            "rounds": rounds,
            "outcome": "stopped" if stopped else ("dry_run" if dry_run else "shipped"),
            "stopped": stopped,
            "pull_request": pull,
            "board_error": board_error,
            "escalation": escalation,
            "report_path": str(report_path),
            "total_cost_usd": trace.cost_by_pipeline_run(run_id),
            "duration_ms": round((time.monotonic() - start) * 1000),
        }
        report_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    code = ExitCode.OK if stopped is None else ExitCode(stopped["exit_code"])
    return BuildResult(document=document, code=code)


def _last_outputs(agents: BuildAgents, rounds: Sequence[Mapping[str, Any]]) -> list[AgentOutput]:
    """Return the last round's coder answer and panel result, the outputs an escalation keeps."""
    if not rounds:
        return []
    last = rounds[-1]
    found = []
    if last.get("coder") is not None:
        coder = last["coder"]
        found.append(AgentOutput.of(agents.coder.address, coder["run_id"], coder["output"]))
    if last.get("panel") is not None:
        found.append(AgentOutput.of("review panel", None, last["panel"]))
    return found


def _last_suite_tail(rounds: Sequence[Mapping[str, Any]]) -> str:
    """Return the tail of the last suite run's output, or an empty string when none ran."""
    for entry in reversed(rounds):
        if entry.get("suite") is not None:
            return str(entry["suite"]["output_tail"])
    return ""


def _capture_diff(clone: Path, head_sha: str) -> str:
    """Return the clone's change from `head_sha`, uncommitted work included, or "" when git fails.

    Called only on a stop, so it commits what is left in the throwaway clone and never raises.
    """
    try:
        sha = commit_all(clone, "work left at the stop")
        return diff_patch(clone, head_sha, sha).decode("utf-8", errors="replace")
    except FactoryError:
        return ""


def _build(
    agents: BuildAgents,
    item: WorkItem,
    ticket: Ticket,
    depends_on: list[str],
    trace: Trace,
    *,
    root: Path,
    head_sha: str,
    context: OutsideContext,
    work_dir: str | Path | None,
    sandbox: Sandbox,
    runner: Runner,
    jev_runner: JevRunner,
    run_id: str,
    budget: Budget,
    rounds: list[dict[str, Any]],
    workspace: dict[str, str],
) -> dict[str, Any]:
    """Run the rounds in a clone and open the pull request; return the report's `pull_request`.

    Appends each round to `rounds` as it goes, so a stop keeps what ran. On a `FactoryError` stop it
    sets `workspace["diff"]` to the clone's change from `head_sha` before the clone goes.
    """
    coder = agents.coder
    policy = FilePolicy(_coder_files(coder))
    check = KIND_CHECKS[item.kind]() if item.kind in KIND_CHECKS else None

    def refuse(agent: str, path: str, reason: str, check: str) -> FileWriteRefusedError:
        return _refuse(trace, run_id, agent, path, reason, check)

    with checkout(root, head_sha, work_dir=work_dir) as clone:
        try:
            asked = ticket
            parent: str | None = None
            previous = head_sha
            if check is not None:
                check.before(clone, sandbox, head_sha)
            for number in range(1, MAX_ROUNDS + 1):
                entry: dict[str, Any] = {
                    "number": number,
                    "coder": None,
                    "commit": None,
                    "files_changed": [],
                    "suite": None,
                    "panel": None,
                }
                rounds.append(entry)
                coder_run_id = str(uuid.uuid4())
                output = runner(
                    coder,
                    coder.input_class.model_validate(
                        {
                            "repo_path": str(clone),
                            "ticket": asked.model_dump(mode="json"),
                            "depends_on": depends_on,
                        }
                    ),
                    trace,
                    repo_path=clone,
                    pipeline_run_id=run_id,
                    parent_run_id=parent,
                    run_id=coder_run_id,
                    hook=FilePolicyGuard(clone, policy, refuse),
                    budget=budget,
                )
                answer = output.model_dump(mode="json")
                entry["coder"] = {"run_id": coder_run_id, "output": answer}
                # The commits carry no bytecode, and `clean` deletes it before the suite runs.
                check_changed(clone, policy, coder.address, refuse)
                if answer.get("blocked"):
                    raise CoderBlockedError(
                        f"the coder is blocked on {item.id}", hint=answer.get("blocked_reason")
                    )
                sha = commit_all(clone, f"{ticket.title}\n\nWork item {item.id}, round {number}.")
                changed = changed_between(clone, previous, sha)
                entry["commit"] = sha
                entry["files_changed"] = [
                    {"path": path, "action": action} for path, action in changed.items()
                ]
                if not changed:
                    raise NoChangeError(
                        f"the coder changed nothing in round {number}",
                        hint="check the ticket still asks for a change at its ref",
                    )
                previous = sha
                if check is not None:
                    check.after_commit(changed)

                clean(clone)
                suite = sandbox.run(clone, PYTEST)
                entry["suite"] = _suite_json(suite)
                if suite.exit_code != 0:
                    raise SuiteFailedError(
                        f"the test suite failed in the sandbox with exit code {suite.exit_code}",
                        hint=_tail(suite.output)[-400:].strip() or None,
                    )
                if check is not None:
                    check.after_suite(clone, sandbox, entry)

                panel = run_panel(
                    agents.panel,
                    clone,
                    head_sha,
                    sha,
                    trace,
                    runner=runner,
                    jev_runner=jev_runner,
                    pipeline_run_id=run_id,
                    budget=budget,
                    parent_run_id=coder_run_id,
                )
                entry["panel"] = panel.to_json()
                if panel.ship:
                    break
                if number == MAX_ROUNDS:
                    raise ChangesRequestedError(
                        f"the panel asked for changes {MAX_ROUNDS} times",
                        hint=f"read the panel's findings in the report, then move {item.id} "
                        "back to ready",
                    )
                asked = findings_ticket(ticket, _findings(panel))
                parent = _judge_run_id(trace, panel) or coder_run_id

            _fetch_into(root, clone, previous)
            body = pr_body(item, ticket, output)
            done = write(
                "github.create_pr",
                {"title": ticket.title, "body": body, "commit": previous},
                agent=agents.panel.judge,
                context=dataclasses.replace(context, parent_run_id=_judge_run_id(trace, panel)),
            )
        except FactoryError:
            workspace["diff"] = _capture_diff(clone, head_sha)
            raise
        finally:
            sandbox.cleanup(clone)
    return {
        "outcome": "dry_run" if context.dry_run else "written",
        "action": done["action"],
        "url": done["url"],
        "branch": branch_for(run_id),
        "commit": previous,
        "title": ticket.title,
        "body": done["body"],
        "patch": None,
    }


def command(
    item_id: str,
    *,
    repo: str | None = None,
    dry_run: bool = False,
    out_dir: str | None = None,
    work_dir: str | None = None,
    budget_usd: float | None = None,
    as_json: bool = False,
    allow_unpublished: bool = False,
) -> int:
    """Run `factory build`: build one `ready` work item and open its pull request.

    Checks the item, the agents and both keys (`ANTHROPIC_API_KEY`, and `GH_TOKEN`, which a dry
    run needs too) before anything runs: exit 3 for an unknown item, 2 for one that is not
    buildable, 4 for a missing key. Prose goes to stderr under `as_json`, where stdout gets exactly
    one JSON document, the report, however the run ended after preflight. Without it the text
    report goes to stdout. Returns 0 when the pull request was opened (or planned, in a dry run)
    and the stop's exit code otherwise. `budget_usd` caps the spend of the whole run (default:
    `DEFAULT_BUDGET_USD`).
    """
    with Trace() as trace:
        item = WorkItems(trace).get(item_id)
        check_buildable(item)
        agents = resolve_build_agents(item.kind, allow_unpublished=allow_unpublished)
        for key in (API_KEY_ENV, GH_TOKEN_ENV):
            if not os.environ.get(key, "").strip():
                raise AuthOrConfigError(
                    f"{key} is not set",
                    hint=f"store it with `agb sys secrets set {key}` and export it",
                )
        budget = Budget(DEFAULT_BUDGET_USD if budget_usd is None else budget_usd)
        result = run_build(
            agents,
            item_id,
            trace,
            repo_path=repo,
            dry_run=dry_run,
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
    return result.code
