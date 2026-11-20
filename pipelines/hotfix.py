"""The hotfix pipeline: take one reproduced incident ticket to a reviewed pull request, and stop.

`resolve_hotfix_agents` pins the agents before anything runs, `run_hotfix` runs them under one
pipeline run id and `command` is what `factory hotfix` calls. The ticket comes from
`factory incident` (#108), whose body carries the reproduction test; that test is the finding the
fix loop works on. The pipeline ends at a pull request labelled `LABEL`: a person merges it and
deploys, and the factory has no deploy tool and makes no merge call (ADR 0004). The flow
(docs/hotfix.md has the options and outputs):

1. Preflight, with the work item untouched: the item must be a `ready` ticket of kind `incident`
   whose body says the event reproduced (`pipelines.incident.is_reproduced`) and carries the test's
   code, the repository clean, its `origin` on github.com (ADR 0003) and, when the ticket names a
   repository, the same one. Any failure here raises and writes nothing.
2. The pipeline run is linked to the item and, unless it is a dry run, the item moves to
   `in_progress` (ADR 0002), before the clone is made.
3. In a throwaway clone (`pipelines.sandbox.checkout`) at the ticket's `ref`, the release the
   event came from, the reproduction test is written and committed, and it runs in the sandbox. It
   must still fail the way the event did (`evals.graders.incident.reproduces`), or the run stops
   before any model is called.
4. `pipelines.fix_loop.fix_and_review`, the stage `factory fix` runs, does the rest: the fixer
   changes the code behind its `files` policy, with the reproduction test protected; the fixer's
   work is committed, the whole suite runs in the sandbox and the review panel reviews the change
   from the ticket's `ref` to the fixer's commit. There is one round: when the panel asks for
   changes the run stops with `HotfixChangesRequestedError`.
5. When the panel ships, the commit is fetched into the source repository's object store (no ref is
   made) and `runtime.outside.write("github.create_pr", ...)` pushes the branch
   `factory/<pipeline_run_id>` and opens the pull request with the `hotfix` label, for the judge
   manifest and with the judge's row as the parent. The item moves to `in_review`.

Every other end, a stop or a crash, goes through `_stop_item`, which moves the item to `blocked`
with `<ErrorClass>: <message>; report <path>`. A dry run runs every step, reads included (the
label check too), but makes no outside write and moves no item. The report,
`<pipeline run id>.json`, is written last, however the run ended after preflight, and records the
time from the event to the pull request.
"""

import dataclasses
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from evals.graders.incident import REPRODUCE_ARGS, reproduces
from evals.graders.sandbox_tests import PYTEST
from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from pipelines.build_loop import (
    GH_TOKEN_ENV,
    BuildError,
    _fetch_into,
    _judge_run_id,
    _pick_repo,
    _stop_json,
    _suite_json,
)
from pipelines.fix_loop import FIXER, FixStage, _fixer_files, _resolve_worker, fix_and_review
from pipelines.ids import new_pipeline_run_id
from pipelines.incident import incident_sections, is_reproduced
from pipelines.review_panel import JevRunner, Panel, Runner, resolve_panel
from pipelines.sandbox import DockerSandbox, Sandbox, checkout, clean, commit_all
from registry.common_schemas import Finding, Ticket
from registry.spec import LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget
from runtime.jev_runner import run_jev
from runtime.outside import OutsideContext, write
from runtime.runner import API_KEY_ENV, run
from runtime.trace import Trace
from runtime.work_items import WorkItem, WorkItems
from tools._common import resolve_commit
from tools.github import branch_for

ACTOR = "pipeline:hotfix"
"""The actor of every move the hotfix pipeline makes on a work item."""

DEFAULT_OUT_DIR = Path("data/hotfixes")
"""Where the report goes, relative to the working directory like the trace database."""

LABEL = "hotfix"
"""The label the pull request carries. It must exist on GitHub: the factory never creates one."""

PROTECTED_REASON = "the incident's reproduction test"
"""Why the fixer may not change a reproduction test file, as its refusal says."""

_CULPRIT = re.compile(r"^`(.+?)` lines? (\d+)(?:-(\d+))?: (.*)$", re.DOTALL)
_TEST_FILE = re.compile(r"^`([^`]+)`:$")
_PYTHON_FENCE = re.compile(r"^(`{3,})python$")


class HotfixError(BuildError):
    """The hotfix pipeline could not produce a fix worth opening a pull request for (exit 1)."""


class HotfixChangesRequestedError(HotfixError):
    """The review panel asked for changes, and a hotfix gets one round."""


@dataclass(frozen=True, slots=True)
class HotfixAgents:
    """The resolved agents of one hotfix run: the fixer and the review panel."""

    fixer: LLMSpec
    panel: Panel


@dataclass(frozen=True, slots=True)
class IncidentPlan:
    """What the pipeline reads from an incident ticket's body before it runs anything.

    `code` maps each reproduction test file to its code, in the body's order. `culprit` is the
    file, first and last line and the reason from the Culprit section. `first_seen` and
    `last_seen` are the Events bullet's times, as written, or None when the body has none.
    """

    sections: dict[str, str]
    code: tuple[tuple[str, str], ...]
    exception: str
    crash_file: str
    level: str
    culprit: tuple[str, int, int, str]
    first_seen: str | None
    last_seen: str | None

    @property
    def test_files(self) -> tuple[str, ...]:
        """Return the reproduction test files, in the body's order."""
        return tuple(path for path, _ in self.code)


def resolve_hotfix_agents(
    *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> HotfixAgents:
    """Resolve the fixer and the full review panel with triage and judge gate.

    Unless `allow_unpublished` is set, only published versions resolve. Raises `UsageError` when
    the fixer is not an LLM agent sandboxed offline or has no action guard or no `files` policy,
    and the resolver's `NotFoundError` subclasses (exit 3) when an agent or version is missing.
    """
    fixer = _resolve_worker(*FIXER, allow_unpublished, trace_path)
    if fixer.guard is None:
        raise UsageError(
            f"{fixer.address} has no action guard, so the hotfix pipeline will not run it",
            hint="set `guard` in its manifest",
        )
    _fixer_files(fixer)
    panel = resolve_panel(allow_unpublished=allow_unpublished, trace_path=trace_path)
    return HotfixAgents(fixer=fixer, panel=panel)


def _bullets(section: str) -> dict[str, str]:
    """Return the `- Key: value` bullets of a section, the first of each key."""
    found: dict[str, str] = {}
    for line in section.splitlines():
        if line.startswith("- ") and ": " in line:
            key, value = line[2:].split(": ", 1)
            found.setdefault(key.strip(), value.strip())
    return found


def _test_code(section: str) -> tuple[tuple[str, str], ...]:
    """Return `(path, code)` for each `` `<path>`: `` line followed by a `python` fence."""
    lines = section.splitlines()
    code: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        named = _TEST_FILE.match(lines[index])
        index += 1
        if named is None:
            continue
        while index < len(lines) and not lines[index].strip():
            index += 1
        fence = _PYTHON_FENCE.match(lines[index]) if index < len(lines) else None
        if fence is None:
            continue
        end = index + 1
        while end < len(lines) and lines[end] != fence.group(1):
            end += 1
        if end == len(lines):
            raise ValueError(f"the code of {named.group(1)} has no closing fence")
        code.append((named.group(1), "\n".join(lines[index + 1 : end]) + "\n"))
        index = end + 1
    return tuple(code)


def _unsafe(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return path.startswith("/") or "\\" in path or ".." in parts or ".git" in parts or not parts


def check_hotfixable(item: WorkItem) -> IncidentPlan:
    """Return what the pipeline needs from `item`'s body, or raise `UsageError` (exit 2).

    The item must be a `ready` ticket of kind `incident` whose body has the layout
    `pipelines.incident.BODY_SECTIONS`, says `- Reproduced: yes`, names the exception and the
    crash file, and carries the code of at least one reproduction test file at a relative path
    outside `.git`. An incident that did not reproduce goes to a person, not to this pipeline.
    """
    if item.kind != "incident":
        raise UsageError(
            f"{item.id} is a {item.kind} ticket, and the hotfix pipeline takes only incidents",
            hint="use `factory build` for it",
        )
    if item.status != "ready":
        raise UsageError(
            f"{item.id} is {item.status}, and the hotfix pipeline takes only ready items",
            hint="see what is ready with `factory ticket ready`",
        )
    body = item.ticket().body
    try:
        sections = incident_sections(body)
        reproduced = is_reproduced(body)
        code = _test_code(sections["Reproduction test"])
    except ValueError as exc:
        raise UsageError(
            f"{item.id} does not have an incident body: {exc}",
            hint="file incidents with `factory incident`",
        ) from exc
    unreproduced_hint = (
        "a person has to reproduce it first; run `factory incident` again or fix it by hand"
    )
    if not reproduced:
        raise UsageError(
            f"{item.id} was not reproduced, so there is no test for a hotfix to pass",
            hint=unreproduced_hint,
        )
    if not code:
        raise UsageError(
            f"{item.id} says it was reproduced but carries no test code", hint=unreproduced_hint
        )
    for path, _ in code:
        if _unsafe(path):
            raise UsageError(f"{item.id} names a reproduction test at an unsafe path: {path}")
    event = _bullets(sections["Event"])
    exception = event.get("Exception", "").split(": ", 1)[0].strip()
    crash_file = event.get("Crash file", "unknown")
    if not exception or crash_file == "unknown":
        raise UsageError(
            f"{item.id} does not name the exception and the crash file of its event",
            hint=unreproduced_hint,
        )
    culprit = _CULPRIT.match(sections["Culprit"])
    if culprit is None:
        raise UsageError(f"{item.id} has a Culprit section the hotfix pipeline cannot read")
    first = int(culprit.group(2))
    events = event.get("Events", "")
    return IncidentPlan(
        sections=sections,
        code=code,
        exception=exception,
        crash_file=crash_file,
        level=event.get("Level", "unknown"),
        culprit=(culprit.group(1), first, int(culprit.group(3) or first), culprit.group(4)),
        first_seen=_seen(events, "first"),
        last_seen=_seen(events, "last"),
    )


def _seen(events: str, which: str) -> str | None:
    found = re.search(rf"{which} seen ([^,;]+)", events)
    return found.group(1).strip() if found else None


def hotfix_finding(ticket: Ticket, plan: IncidentPlan) -> Finding:
    """Return the finding the fixer works on: the incident's culprit, with the test as its proof.

    Severity is `blocker` for a `fatal` event and `major` otherwise, category `crash`, as
    `factory incident` files it. The body has the summary, the stack trace, the reproduction test
    and the rule that the code changes, not the test.
    """
    file, line_start, line_end, reason = plan.culprit
    files = ", ".join(f"`{path}`" for path in plan.test_files)
    body = "\n\n".join(
        [
            plan.sections["Summary"],
            f"Culprit: {reason}",
            "The stack trace of the event:",
            plan.sections["Stack trace"],
            plan.sections["Reproduction test"],
            (
                f"{files} fails at this commit with {plan.exception} raised from "
                f"`{plan.crash_file}`, the way the event did. Make it pass by changing the code, "
                "not the test, and keep the whole suite passing."
            ),
        ]
    )
    return Finding(
        file=file,
        line_start=line_start,
        line_end=line_end,
        severity="blocker" if plan.level == "fatal" else "major",
        category="crash",
        title=ticket.title,
        body=body,
    )


def event_time(plan: IncidentPlan) -> tuple[datetime | None, str | None]:
    """Return when the incident's latest event happened and which bullet said so.

    That is `last seen`, else `first seen`, from the Event section's Events bullet. A time without
    a zone is taken as UTC. Returns `(None, None)` when neither is there or neither parses.
    """
    for source, text in (("last_seen", plan.last_seen), ("first_seen", plan.first_seen)):
        if text is None:
            continue
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            continue
        return (when if when.tzinfo else when.replace(tzinfo=UTC)), source
    return None, None


def hotfix_pr_body(item: WorkItem, ticket: Ticket, fixer_output: BaseModel) -> str:
    """Return the pull request body: the ticket, its acceptance, what the fixer did, the item.

    `github.create_pr` adds the pipeline run and its marker below it.
    """
    out = fixer_output.model_dump(mode="json")
    lines = [ticket.body.rstrip(), "", "## Acceptance", ""]
    lines.extend(f"- {criterion}" for criterion in ticket.acceptance)
    lines += ["", "## What the fixer did", "", str(out.get("summary", "")).strip()]
    files = out.get("files_changed") or []
    if files:
        lines.append("")
        lines.extend(f"- {f.get('action')} `{f.get('path')}`: {f.get('summary')}" for f in files)
    lines += ["", f"Work item `{item.id}`. A person merges and deploys this; the factory does not."]
    return "\n".join(lines).strip() + "\n"


def _stop_item(
    items: WorkItems,
    item_id: str,
    stopped: Mapping[str, Any] | None,
    report_path: Path,
    *,
    pipeline_run_id: str,
) -> dict[str, Any] | None:
    """Move the item to `blocked` for a run that did not open its pull request.

    The reason is `<ErrorClass>: <message>; report <path>`. Returns the stop of a board move that
    failed, for the report, or None. Every stop goes through here, so when the escalation API of
    #93 lands it replaces this one function.
    """
    cause = (
        f"{stopped['type']}: {stopped['message']}"
        if stopped is not None
        else "the hotfix did not finish"
    )
    try:
        items.move(
            item_id,
            "blocked",
            f"{cause}; report {report_path}",
            actor=ACTOR,
            pipeline_run_id=pipeline_run_id,
        )
    except FactoryError as exc:
        return _stop_json(exc)
    return None


@dataclass(frozen=True, slots=True)
class HotfixResult:
    """What one hotfix run produced: the report `factory hotfix` writes and prints, and its code."""

    document: dict[str, Any]
    code: ExitCode

    def to_json(self) -> dict[str, Any]:
        """Return the report, as written to `report_path`."""
        return self.document

    def text(self) -> str:
        """Return a short plain-text report of the run, for use without `--json`."""
        doc = self.document
        item = doc["work_item"]
        lines = [f"hotfix run {doc['pipeline_run_id']}: {item['id']} {item['ticket']['title']}"]
        repro = doc["reproduction"]
        if repro["run"] is not None:
            state = "still reproduces" if repro["reproduced"] else "does not reproduce"
            lines.append(f"  reproduction {', '.join(repro['test_files'])} {state}")
        fix = doc["fix"]
        if fix is not None:
            parts = [f"fix: {len(fix['files_changed'])} files changed"]
            parts.append(f"suite passed in {fix['suite']['seconds']:.1f}s")
            parts.append("panel ships" if fix["panel"]["ship"] else "panel asks for changes")
            lines.append("  " + "; ".join(parts))
        pull = doc["pull_request"]
        if pull is not None and pull["url"] is not None:
            lines.append(f"pull request {pull['url']} (label {LABEL}; a person merges and deploys)")
        elif pull is not None:
            lines.append(f"dry run: would open a pull request from {pull['branch']}")
        if doc["stopped"] is not None:
            lines.append(f"stopped: {doc['stopped']['message']}")
            if doc["stopped"]["hint"]:
                lines.append(f"  hint: {doc['stopped']['hint']}")
        seconds = doc["timing"]["time_to_pr_seconds"]
        if seconds is not None:
            lines.append(f"time from event to pull request {seconds / 60:.1f} min")
        lines.append(f"work item {item['id']}: {item['status_before']} -> {item['status_after']}")
        if doc["board_error"] is not None:
            lines.append(f"  could not move the work item: {doc['board_error']['message']}")
        lines.append(f"report {doc['report_path']}")
        lines.append(f"cost ${doc['total_cost_usd']:.4f}")
        return "\n".join(lines)


def run_hotfix(
    agents: HotfixAgents,
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
) -> HotfixResult:
    """Fix the reproduced incident `item_id` and, when the panel ships it, open its pull request.

    The repository is `repo_path`, else the ticket's `repo` when it is a path, else the working
    directory; the fix starts from the ticket's `ref`. Raises, before the item is touched,
    `WorkItemNotFound` (exit 3), and `UsageError` (exit 2) for an item `check_hotfixable` refuses,
    a dirty repository, an `origin` that is not on github.com or is not the ticket's repository, a
    `repo_path` that is not the ticket's, or a bad `work_dir` or `out_dir`; the sandbox's
    `ToolUnavailableError` (exit 4) before any agent runs.

    After that nothing is raised for a `FactoryError`: the run stops, `_stop_item` moves the item
    to `blocked` (not in a dry run) and the result carries the stop and its exit code. A missing
    `hotfix` label is such a stop: `github.create_pr` refuses with `OutsideWriteRefused` (exit 1),
    in a dry run too. Any other exception is recorded the same way and then raised. The report goes
    to `out_dir` on every end after preflight. Every agent call and the outside write share
    `pipeline_run_id` and are charged to `budget` (default: `DEFAULT_BUDGET_USD`). The result's
    code is 0 when the pull request was opened, or would have been in a dry run.
    """
    start = time.monotonic()
    run_id = pipeline_run_id or new_pipeline_run_id()
    budget = budget or Budget(DEFAULT_BUDGET_USD)
    sandbox = sandbox or DockerSandbox()
    items = WorkItems(trace)
    item = items.get(item_id)
    plan = check_hotfixable(item)
    ticket = item.ticket()
    root = _pick_repo(ticket, repo_path)
    head_sha = resolve_commit(root, ticket.ref)
    context = OutsideContext.for_repository(
        root, pipeline_run_id=run_id, trace=trace, dry_run=dry_run
    )
    if not ticket.repo.startswith("/") and not context.target.same(ticket.repo):
        raise UsageError(
            f"origin is {context.target.full_name}, the ticket's repository is {ticket.repo}",
            hint="run the hotfix in a clone of the ticket's repository",
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
    finding = hotfix_finding(ticket, plan)
    reproduction: dict[str, Any] = {
        "test_files": list(plan.test_files),
        "exception": plan.exception,
        "crash_file": plan.crash_file,
        "commit": None,
        "reproduced": None,
        "run": None,
    }
    fix: dict[str, Any] | None = None
    pull: dict[str, Any] | None = None
    stopped: dict[str, Any] | None = None
    board_error: dict[str, Any] | None = None
    opened_at: datetime | None = None
    shipped = False
    try:
        items.link_run(item.id, run_id)
        if not dry_run:
            items.move(
                item.id, "in_progress", "hotfix started", actor=ACTOR, pipeline_run_id=run_id
            )
        with checkout(root, head_sha, work_dir=work_dir) as clone:
            try:
                stage = _reproduce_and_fix(
                    agents,
                    clone,
                    plan,
                    finding,
                    trace,
                    head_sha=head_sha,
                    sandbox=sandbox,
                    runner=runner,
                    jev_runner=jev_runner,
                    run_id=run_id,
                    budget=budget,
                    reproduction=reproduction,
                )
                fix = _fix_json(stage)
                if not stage.panel.ship:
                    raise HotfixChangesRequestedError(
                        "the panel asked for changes, and a hotfix gets one round",
                        hint=f"read the panel's findings in the report, then move {item.id} "
                        "back to ready or fix it by hand",
                    )
                _fetch_into(root, clone, stage.fix_sha)
                done = write(
                    "github.create_pr",
                    {
                        "title": ticket.title,
                        "body": hotfix_pr_body(item, ticket, stage.fixer_output),
                        "commit": stage.fix_sha,
                        "labels": [LABEL],
                    },
                    agent=agents.panel.judge,
                    context=dataclasses.replace(
                        context, parent_run_id=_judge_run_id(trace, stage.panel)
                    ),
                )
            finally:
                sandbox.cleanup(clone)
        pull = {
            "outcome": "dry_run" if dry_run else "written",
            "action": done["action"],
            "url": done["url"],
            "branch": branch_for(run_id),
            "commit": stage.fix_sha,
            "title": ticket.title,
            "labels": [LABEL],
            "body": done["body"],
        }
        if not dry_run:
            opened_at = datetime.now(UTC)
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
    finally:
        if not dry_run and not shipped:
            board_error = _stop_item(items, item.id, stopped, report_path, pipeline_run_id=run_id)
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
            "fixer": agents.fixer.address,
            "finding": finding.model_dump(mode="json"),
            "reproduction": reproduction,
            "fix": fix,
            "outcome": "stopped" if stopped else ("dry_run" if dry_run else "shipped"),
            "stopped": stopped,
            "pull_request": pull,
            "board_error": board_error,
            "timing": _timing(plan, opened_at),
            "report_path": str(report_path),
            "total_cost_usd": trace.cost_by_pipeline_run(run_id),
            "duration_ms": round((time.monotonic() - start) * 1000),
        }
        report_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    code = ExitCode.OK if stopped is None else ExitCode(stopped["exit_code"])
    return HotfixResult(document=document, code=code)


def _reproduce_and_fix(
    agents: HotfixAgents,
    clone: Path,
    plan: IncidentPlan,
    finding: Finding,
    trace: Trace,
    *,
    head_sha: str,
    sandbox: Sandbox,
    runner: Runner,
    jev_runner: JevRunner,
    run_id: str,
    budget: Budget,
    reproduction: dict[str, Any],
) -> FixStage:
    """Commit the reproduction test, check it still fails the event's way, then fix and review.

    Fills `reproduction` as it goes, so a stop keeps what ran.
    """
    for path, code in plan.code:
        target = clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
    tests_sha = commit_all(clone, "hotfix: reproduction test")
    reproduction["commit"] = tests_sha
    clean(clone)
    result = sandbox.run(clone, [*PYTEST, *REPRODUCE_ARGS, *plan.test_files])
    output = result.output.decode("utf-8", errors="replace")
    reproduction["run"] = _suite_json(result)
    reproduction["reproduced"] = reproduces(
        result.exit_code, output, plan.exception, plan.crash_file
    )
    if not reproduction["reproduced"]:
        raise HotfixError(
            f"the reproduction test no longer fails with {plan.exception} raised from "
            f"{plan.crash_file} (exit code {result.exit_code})",
            hint="the ticket's ref or the test changed; run `factory incident` again",
        )
    return fix_and_review(
        agents.fixer,
        agents.panel,
        clone,
        finding,
        trace,
        base_sha=head_sha,
        tests_sha=tests_sha,
        test_files=plan.test_files,
        protected=dict.fromkeys(plan.test_files, PROTECTED_REASON),
        sandbox=sandbox,
        runner=runner,
        jev_runner=jev_runner,
        pipeline_run_id=run_id,
        budget=budget,
        fixer_must_change=True,
    )


def _fix_json(stage: FixStage) -> dict[str, Any]:
    return {
        "fixer": {
            "run_id": stage.fixer_run_id,
            "output": stage.fixer_output.model_dump(mode="json"),
        },
        "commit": stage.fix_sha,
        "files_changed": [
            {
                "path": path,
                "action": action,
                "by": "fixer" if path in stage.by_fixer else "reproduction",
            }
            for path, action in stage.total.items()
        ],
        "suite": _suite_json(stage.suite),
        "panel": stage.panel.to_json(),
    }


def _timing(plan: IncidentPlan, opened_at: datetime | None) -> dict[str, Any]:
    when, source = event_time(plan)
    seconds = (
        round((opened_at - when).total_seconds(), 3)
        if when is not None and opened_at is not None
        else None
    )
    return {
        "event_time": when.isoformat() if when is not None else None,
        "event_time_source": source,
        "pr_opened_at": opened_at.isoformat() if opened_at is not None else None,
        "time_to_pr_seconds": seconds,
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
    """Run `factory hotfix`: fix one reproduced incident and open its `hotfix` pull request.

    Checks the item, the agents and both keys (`ANTHROPIC_API_KEY`, and `GH_TOKEN`, which a dry
    run needs too) before anything runs: exit 3 for an unknown item, 2 for one `check_hotfixable`
    refuses, 4 for a missing key. Prose goes to stderr under `as_json`, where stdout gets exactly
    one JSON document, the report, however the run ended after preflight. Without it the text
    report goes to stdout. Returns 0 when the pull request was opened (or planned, in a dry run)
    and the stop's exit code otherwise. `budget_usd` caps the spend of the whole run (default:
    `DEFAULT_BUDGET_USD`). It never merges or deploys.
    """
    with Trace() as trace:
        check_hotfixable(WorkItems(trace).get(item_id))
        agents = resolve_hotfix_agents(allow_unpublished=allow_unpublished)
        for key in (API_KEY_ENV, GH_TOKEN_ENV):
            if not os.environ.get(key, "").strip():
                raise AuthOrConfigError(
                    f"{key} is not set",
                    hint=f"store it with `agb sys secrets set {key}` and export it",
                )
        budget = Budget(DEFAULT_BUDGET_USD if budget_usd is None else budget_usd)
        result = run_hotfix(
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
