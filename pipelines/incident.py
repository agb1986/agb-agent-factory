"""The incident pipeline: from a production event to a reproducible incident ticket (#108).

`resolve_incident_agents` pins the agents before anything runs, `run_incident` handles one event
under one pipeline run id and `command` is what `factory incident` calls. The input is a Sentry
issue in the document shape `sentry.read_issue` returns, given as JSON; the pipeline never calls
Sentry itself. The flow, all of it in a throwaway clone of the repository
(`pipelines.sandbox.checkout`):

1. The release the event came from is found: `--ref` when given, else the event's `release` as a
   tag or commit, else `v<release>`. Without one of those the run stops with `UsageError` before
   any agent runs, so a diagnosis is never made against the wrong code.
2. The clone is checked out at that commit and `triage-diagnostics` reads it with read-only tools:
   it writes the title, the reproduction steps, the suspected paths (`deps.impact` of the culprit),
   the culprit lines, a confidence and a draft test. It writes nothing anywhere.
3. `test-writer` runs unchanged in reproduce mode on a finding built from the diagnosis and the
   event, and its work is committed in the clone.
4. The test files it wrote run in the offline sandbox. They reproduce the event when they fail
   with the event's exception raised from the event's innermost in-app file
   (`evals.graders.incident.reproduces`, the rule the eval applies too). A reproduction raises the
   confidence to at least `REPRODUCED_CONFIDENCE` and its code goes into the ticket.
5. The pipeline files one `incident` ticket in the work-item store with actor `ACTOR`, in `ready`
   when the event reproduced and in `todo` otherwise, links the run to it and writes a JSON report.

The ticket's body has the fixed layout `BODY_SECTIONS` that `docs/incident.md` documents, which the
hotfix pipeline (#109) reads; `incident_sections` splits a body back into its sections. Nothing
is committed to, pushed from or merged into the source repository.

It composes two teams: `sre` (triage-diagnostics) and `qa` (test-writer).
"""

import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from evals.graders.incident import REPRODUCE_ARGS, reproduces
from evals.graders.sandbox_tests import PYTEST
from factory.exit import AuthOrConfigError, ExitCode, NotFoundError, UsageError
from pipelines.ids import new_pipeline_run_id
from pipelines.sandbox import DockerSandbox, Sandbox, changed_between, checkout, clean, commit_all
from registry.common_schemas import Finding, Ticket
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget
from runtime.runner import API_KEY_ENV, run
from runtime.trace import Trace
from runtime.work_items import WorkItem, WorkItems
from tools._common import ToolError, resolve_commit, resolve_root
from tools.sandbox import SandboxResult

Runner = Callable[..., BaseModel]

TRIAGE: tuple[str, str] = ("triage-diagnostics", "^0.1")
"""The agent that diagnoses the event and the version range it is pinned to."""

TEST_WRITER: tuple[str, str] = ("test-writer", "^0.2")
"""The agent that writes the reproduction test and the version range it is pinned to."""

ACTOR = "pipeline:incident"
"""The actor of the ticket's events: ADR 0002's `pipeline:<module>`."""

DEFAULT_OUT_DIR = Path("data/incidents")
"""Where the reports go, relative to the working directory like the trace database."""

REPRODUCED_CONFIDENCE = 0.9
"""The least confidence a ticket has once a test reproduces its event."""

BODY_SECTIONS: tuple[str, ...] = (
    "Event",
    "Summary",
    "Stack trace",
    "Reproduction steps",
    "Suspected paths",
    "Culprit",
    "Confidence",
    "Reproduction test",
)
"""The `## ` headings of an incident ticket's body, in order. Each appears exactly once."""

OUTPUT_TAIL_CHARS = 2000

_HEADING = re.compile(r"^## (.+)$", re.MULTILINE)


class IncidentError(UsageError):
    """The event cannot be triaged as given: no release to read, or a payload that is not one."""


@dataclass(frozen=True, slots=True)
class IncidentAgents:
    """The resolved agents of one incident run: the diagnostician and the test-writer."""

    triage: LLMSpec
    test_writer: LLMSpec


def resolve_incident_agents(
    *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> IncidentAgents:
    """Resolve `triage-diagnostics` and `test-writer`; unless `allow_unpublished`, published ones.

    Raises `UsageError` when either is not an LLM agent, when the triage agent may write (its
    manifest must offer read-only tools only, so a diagnosis never changes the clone) or when the
    test-writer is not sandboxed offline, and the resolver's `NotFoundError` subclasses (exit 3)
    when an agent or version is missing.
    """
    found: list[LLMSpec] = []
    for name, version_range in (TRIAGE, TEST_WRITER):
        spec = resolve(
            name, version_range, published_only=not allow_unpublished, trace_path=trace_path
        )
        if not isinstance(spec, LLMSpec):
            raise UsageError(
                f"{spec.address} is not an LLM agent, so the incident run cannot run it"
            )
        found.append(spec)
    triage, test_writer = found
    if triage.sandbox_profile is not None or triage.files is not None:
        raise UsageError(
            f"{triage.address} is sandboxed or has a files policy, so it is not read-only",
            hint="the triage agent takes read-only tools only (agents/triage-diagnostics)",
        )
    if test_writer.sandbox_profile != "offline":
        raise UsageError(
            f"{test_writer.address} is not sandboxed offline, so it cannot work on the repository",
            hint="set `sandbox: true` in its manifest",
        )
    return IncidentAgents(triage=triage, test_writer=test_writer)


# --- the event ---------------------------------------------------------------------------------


def load_event(text: str) -> dict[str, Any]:
    """Return the JSON object `text` holds: a Sentry issue as `sentry.read_issue` returns it.

    `text` is JSON, or `@` followed by the path of a file holding it. Raises `NotFoundError` for a
    missing file, and `UsageError` for an unreadable one, text that is not JSON, or JSON that is
    not an object. The fields are checked by `parse_event`, against the agent's input model.
    """
    raw = text
    if text.startswith("@"):
        path = Path(text[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise NotFoundError(f"--event: no file at {path}") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError(f"--event: cannot read {path}: {exc}") from None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--event is not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise UsageError("--event must be a JSON object: one Sentry issue")
    return document


def parse_event(triage: LLMSpec, document: dict[str, Any]) -> Any:
    """Return `document` as the triage agent's `event` model, or raise `IncidentError`.

    The model is the one `agents/triage-diagnostics/schemas.py` defines, so the pipeline accepts
    exactly what the agent does.
    """
    event_class = triage.input_class.model_fields["event"].annotation
    assert isinstance(event_class, type) and issubclass(event_class, BaseModel)
    try:
        return event_class.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "the event"
        raise IncidentError(f"--event: {where}: {first['msg']}") from None


def release_commit(root: Path, event: Any, ref: str | None) -> tuple[str, str]:
    """Return the release's name and commit: `ref`, else the event's `release` or `v<release>`.

    Raises `IncidentError` (exit 2) when the event names no release, or names one that is neither
    a tag nor a commit of the repository, and no `ref` is given; a `ref` that names no commit
    raises `tools._common.ToolNotFoundError` (exit 3).
    """
    if ref is not None:
        return ref, resolve_commit(root, ref)
    release = event.release
    if not release:
        raise IncidentError(
            "the event names no release, so the code that ran is unknown",
            hint="pass --ref with the release's tag or commit",
        )
    candidates = [release] if release.startswith("v") else [release, f"v{release}"]
    for name in candidates:
        try:
            return name, resolve_commit(root, name)
        except ToolError:
            continue
    raise IncidentError(
        f"the event's release {release!r} is not a tag or commit of {root}",
        hint=f"tried {', '.join(candidates)}; fetch the tags or pass --ref with the release's commit",
    )


def raised_exception(event: Any) -> Any:
    """Return the exception the event raised: the last of its `exceptions`."""
    return event.exceptions[-1]


def crash_file(event: Any, clone: Path) -> str | None:
    """Return the repository path of the file that raised, or None when no frame maps to one.

    The frame is the innermost of the raised exception's frames that Sentry marks `in_app`, or,
    when none is marked, the innermost not marked otherwise. Its path, which comes from the
    production host, maps to the longest trailing part of it that is a file of the clone, so
    `/app/src/reviews/scores.py` finds `src/reviews/scores.py` or `reviews/scores.py`.
    """
    frames = [f for f in raised_exception(event).frames if f.file]
    marked = [f for f in frames if f.in_app]
    candidates = marked or [f for f in frames if f.in_app is not False]
    if not candidates:
        return None
    parts = [p for p in candidates[-1].file.replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        return None
    for start in range(len(parts)):
        relative = "/".join(parts[start:])
        if (clone / relative).is_file():
            return relative
    return None


# --- the ticket body ---------------------------------------------------------------------------


def stack_trace(event: Any) -> str:
    """Render the event's exceptions as a Python traceback, outermost frame first."""
    blocks = []
    for exception in event.exceptions:
        lines = ["Traceback (most recent call last):"]
        for frame in exception.frames:
            where = f'File "{frame.file or "?"}", line {frame.line or "?"}'
            lines.append(f"  {where}, in {frame.function or '?'}")
        message = f": {exception.value}" if exception.value else ""
        lines.append(f"{exception.type}{message}")
        blocks.append("\n".join(lines))
    return "\n\nThe above exception was the cause of the following exception:\n\n".join(blocks)


@dataclass(frozen=True, slots=True)
class Reproduction:
    """What the reproduction run showed: the test files, their code and the sandbox run."""

    test_files: tuple[str, ...]
    code: tuple[tuple[str, str], ...]
    exception: str
    crash_file: str | None
    run: SandboxResult | None
    reproduced: bool
    why_not: str | None

    def to_json(self) -> dict[str, Any]:
        """Return the reproduction for the report."""
        return {
            "test_files": list(self.test_files),
            "exception": self.exception,
            "crash_file": self.crash_file,
            "reproduced": self.reproduced,
            "why_not": self.why_not,
            "run": None
            if self.run is None
            else {
                "argv": list(self.run.argv),
                "exit_code": self.run.exit_code,
                "seconds": round(self.run.seconds, 3),
                "output_tail": _tail(self.run.output),
            },
        }


def _fence(text: str, language: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{language}\n{text.rstrip()}\n{ticks}"


def ticket_body(
    event: Any,
    release: str,
    commit: str,
    diagnosis: Any,
    confidence: float,
    reproduction: Reproduction,
) -> str:
    """Return the ticket's Markdown body: the `BODY_SECTIONS`, in order, each once."""
    raised = raised_exception(event)
    issue = event.short_id or event.id or "unknown"
    seen = ", ".join(
        part
        for part in (
            f"first seen {event.first_seen}" if event.first_seen else "",
            f"last seen {event.last_seen}" if event.last_seen else "",
        )
        if part
    )
    users = f", {event.user_count} users" if event.user_count is not None else ""
    event_lines = [
        f"- Sentry issue: {issue}",
        f"- Title: {event.title}",
        f"- Level: {event.level or 'unknown'}",
        f"- Release: {release} (commit {commit})",
        f"- Exception: {raised.type}" + (f": {raised.value}" if raised.value else ""),
        f"- Crash file: {reproduction.crash_file or 'unknown'}",
        f"- Events: {event.count}{users}" + (f"; {seen}" if seen else ""),
    ]
    if event.event_id:
        event_lines.append(f"- Latest event: {event.event_id}")
    culprit = diagnosis.culprit
    lines = (
        f"line {culprit.line_start}"
        if culprit.line_start == culprit.line_end
        else f"lines {culprit.line_start}-{culprit.line_end}"
    )
    steps = [f"{n}. {step}" for n, step in enumerate(diagnosis.reproduction_steps, start=1)]
    paths = [f"- `{path}`" for path in diagnosis.suspected_paths]
    confidence_lines = [
        f"- Confidence: {confidence:.2f}",
        f"- Reproduced: {'yes' if reproduction.reproduced else 'no'}",
        f"- Diagnosis confidence: {diagnosis.confidence:.2f}",
    ]
    if reproduction.reproduced:
        files = ", ".join(f"`{path}`" for path in reproduction.test_files)
        test = [
            (
                f"{files} fails at {release} with {reproduction.exception} raised from "
                f"`{reproduction.crash_file}`."
            ),
        ]
        for path, code in reproduction.code:
            test.extend(["", f"`{path}`:", "", _fence(code, "python")])
    else:
        test = [f"No test reproduces the event yet: {reproduction.why_not}"]
    sections = {
        "Event": "\n".join(event_lines),
        "Summary": diagnosis.summary.strip(),
        "Stack trace": _fence(stack_trace(event), "text"),
        "Reproduction steps": "\n".join(steps),
        "Suspected paths": "\n".join(paths),
        "Culprit": f"`{culprit.file}` {lines}: {culprit.reason.strip()}",
        "Confidence": "\n".join(confidence_lines),
        "Reproduction test": "\n".join(test),
    }
    return "\n\n".join(f"## {name}\n\n{sections[name]}" for name in BODY_SECTIONS) + "\n"


def incident_sections(body: str) -> dict[str, str]:
    """Split an incident ticket's body into its sections: heading to text, stripped.

    Raises `ValueError` when the `## ` headings outside code fences are not `BODY_SECTIONS` in
    order, so a reader such as the hotfix pipeline never acts on a body it misreads.
    """
    headings: list[tuple[str, int, int]] = []
    fence: str | None = None
    offset = 0
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        match = re.match(r"(`{3,})", stripped)
        if match and (fence is None or match.group(1).startswith(fence) and stripped == match[1]):
            fence = None if fence is not None else match.group(1)
        elif fence is None and (heading := _HEADING.match(line.rstrip("\n"))):
            headings.append((heading.group(1).strip(), offset, offset + len(line)))
        offset += len(line)
    names = tuple(name for name, _, _ in headings)
    if names != BODY_SECTIONS:
        raise ValueError(f"not an incident body: its sections are {list(names)}")
    sections = {}
    for index, (name, _, text_start) in enumerate(headings):
        end = headings[index + 1][1] if index + 1 < len(headings) else len(body)
        sections[name] = body[text_start:end].strip()
    return sections


def is_reproduced(body: str) -> bool:
    """Return whether an incident body says a test reproduces its event (`Reproduced: yes`)."""
    return "- Reproduced: yes" in incident_sections(body)["Confidence"].splitlines()


# --- the run -----------------------------------------------------------------------------------


def _finding(event: Any, diagnosis: Any, crash: str | None) -> Finding:
    raised = raised_exception(event)
    where = f"raised from `{crash}`" if crash else "raised from the application code"
    draft = diagnosis.reproduction_test
    body = "\n\n".join(
        [
            diagnosis.summary.strip(),
            f"Production raised {raised.type}"
            + (f": {raised.value}" if raised.value else "")
            + f", {where}. The stack trace of the event:",
            _fence(stack_trace(event), "text"),
            "Steps that reproduce it:\n"
            + "\n".join(f"{n}. {s}" for n, s in enumerate(diagnosis.reproduction_steps, start=1)),
            (
                f"The test must fail at this commit because {raised.type} is raised from that file, "
                "the way the event shows: call the code with the input the event shows, let the "
                "exception escape (no `pytest.raises`, no `try`), and assert the result a correct "
                "fix gives, so the test passes after the fix."
            ),
            f"A draft of such a test, `{draft.path}`, which may need fixing:",
            _fence(draft.code, "python"),
        ]
    )
    return Finding(
        file=diagnosis.culprit.file,
        line_start=diagnosis.culprit.line_start,
        line_end=diagnosis.culprit.line_end,
        severity="blocker" if event.level == "fatal" else "major",
        category="crash",
        title=diagnosis.title,
        body=body,
    )


def _tail(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")[-OUTPUT_TAIL_CHARS:]


def _reproduce(
    clone: Path,
    base: str,
    written: BaseModel,
    event: Any,
    crash: str | None,
    sandbox: Sandbox,
) -> Reproduction:
    exception = raised_exception(event).type
    head = commit_all(clone, "test-writer: reproduction test")
    changed = changed_between(clone, base, head)
    named = written.model_dump(mode="json").get("test_files", [])
    files = tuple(
        path for path in dict.fromkeys(named) if changed.get(path) in ("created", "modified")
    )

    def missing(why: str, result: SandboxResult | None = None) -> Reproduction:
        return Reproduction(files, (), exception, crash, result, False, why)

    if not files:
        return missing("the test-writer wrote no test file")
    if crash is None:
        return missing("no frame of the event maps to a file of the repository")
    code = tuple((path, (clone / path).read_text(encoding="utf-8")) for path in files)
    clean(clone)
    result = sandbox.run(clone, [*PYTEST, *REPRODUCE_ARGS, *files])
    output = result.output.decode("utf-8", errors="replace")
    if not reproduces(result.exit_code, output, exception, crash):
        return missing(
            f"the test-writer's tests did not fail with {exception} raised from `{crash}` "
            f"(exit code {result.exit_code})",
            result,
        )
    return Reproduction(files, code, exception, crash, result, True, None)


def _acceptance(event: Any, release: str, reproduction: Reproduction) -> list[str]:
    raised = raised_exception(event).type
    if reproduction.reproduced:
        files = ", ".join(reproduction.test_files)
        return [
            f"{files} passes, where it fails at {release} with {raised}.",
            "The whole test suite passes.",
        ]
    return [
        (
            f"A test fails at {release} with {raised} raised from the file the event shows, "
            "and passes after the fix."
        ),
        "The whole test suite passes.",
    ]


@dataclass(frozen=True, slots=True)
class IncidentResult:
    """What one incident run did: the diagnosis, the reproduction and the ticket it filed."""

    pipeline_run_id: str
    repo_path: str
    release: str
    commit: str
    event: Any
    triage: LLMSpec
    diagnosis: BaseModel
    test_writer: LLMSpec
    test_writer_output: BaseModel
    reproduction: Reproduction
    confidence: float
    item: WorkItem
    report_path: Path
    total_cost_usd: float
    duration_ms: int

    def to_json(self) -> dict[str, Any]:
        """Return the report: the event, both agents' answers, the reproduction and the ticket."""
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "repo_path": self.repo_path,
            "release": self.release,
            "commit": self.commit,
            "event": self.event.model_dump(mode="json"),
            "triage": {
                "ref": self.triage.address,
                "output": self.diagnosis.model_dump(mode="json"),
            },
            "test_writer": {
                "ref": self.test_writer.address,
                "output": self.test_writer_output.model_dump(mode="json"),
            },
            "reproduction": self.reproduction.to_json(),
            "confidence": self.confidence,
            "ticket": {
                "id": self.item.id,
                "status": self.item.status,
                **self.item.ticket().model_dump(mode="json"),
            },
            "report_path": str(self.report_path),
            "total_cost_usd": self.total_cost_usd,
            "duration_ms": self.duration_ms,
        }

    def text(self) -> str:
        """Return a short plain-text report of the run, for use without `--json`."""
        reproduction = self.reproduction
        lines = [
            f"incident run {self.pipeline_run_id}: {self.item.title}",
            f"  release {self.release} ({self.commit[:12]})",
            f"  suspected: {', '.join(self.item.impacted_paths)}",
            f"  confidence {self.confidence:.2f}",
        ]
        if reproduction.reproduced:
            lines.append(f"  reproduced by {', '.join(reproduction.test_files)}")
        else:
            lines.append(f"  not reproduced: {reproduction.why_not}")
        lines.append(f"filed {self.item.id} ({self.item.status})")
        lines.append(f"report {self.report_path}")
        lines.append(f"cost ${self.total_cost_usd:.4f}")
        return "\n".join(lines)


def run_incident(
    agents: IncidentAgents,
    repo_path: str | Path,
    event: Any,
    trace: Trace,
    *,
    ref: str | None = None,
    repository: str | None = None,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    work_dir: str | Path | None = None,
    sandbox: Sandbox | None = None,
    runner: Runner = run,
    pipeline_run_id: str | None = None,
    budget: Budget | None = None,
    now: Callable[[], datetime] | None = None,
) -> IncidentResult:
    """Diagnose `event` (a `parse_event` model) in the repository at `repo_path` and file a ticket.

    The release is `release_commit`'s. The agents work in a clone of it under `work_dir` (default:
    the system temporary directory), deleted at the end; the source work tree is never read, so it
    may have uncommitted changes. `sandbox` (default: `DockerSandbox`) runs the reproduction and
    is told to stop whatever still runs over the clone, however the run ends. Every agent call
    gets the same `pipeline_run_id` and is charged to `budget` (default: `DEFAULT_BUDGET_USD`).

    The ticket's `repo` is `repository` (default: the repository's absolute path), its `ref` the
    release commit and its `impacted_paths` the suspected paths. It is filed only when both agents
    finished; a failed run files nothing. Raises `UsageError` for a `work_dir` that is not a
    directory or whose path has a `:`, or an `out_dir` that cannot be made, `IncidentError` for a
    release that cannot be found, the sandbox's `ToolUnavailableError` before any agent runs, and
    whatever an agent's run raises, `BudgetExceeded` among them.
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
    root = resolve_root(repo_path)
    release, commit = release_commit(root, event, ref)
    sandbox.check()
    with checkout(root, commit, work_dir=work_dir) as clone:
        try:
            triage_run_id = str(uuid.uuid4())
            diagnosis: Any = runner(
                agents.triage,
                agents.triage.input_class.model_validate(
                    {
                        "repo_path": str(clone),
                        "event": event.model_dump(mode="json"),
                        "ref": commit,
                    }
                ),
                trace,
                repo_path=clone,
                pipeline_run_id=run_id,
                run_id=triage_run_id,
                budget=budget,
            )
            crash = crash_file(event, clone)
            finding = _finding(event, diagnosis, crash)
            written = runner(
                agents.test_writer,
                agents.test_writer.input_class.model_validate(
                    {
                        "repo_path": str(clone),
                        "mode": "reproduce",
                        "finding": finding.model_dump(mode="json"),
                    }
                ),
                trace,
                repo_path=clone,
                pipeline_run_id=run_id,
                parent_run_id=triage_run_id,
                run_id=str(uuid.uuid4()),
                budget=budget,
            )
            reproduction = _reproduce(clone, commit, written, event, crash, sandbox)
        finally:
            sandbox.cleanup(clone)

    confidence = diagnosis.confidence
    if reproduction.reproduced:
        confidence = max(confidence, REPRODUCED_CONFIDENCE)
    ticket = Ticket(
        kind="incident",
        title=diagnosis.title,
        body=ticket_body(event, release, commit, diagnosis, confidence, reproduction),
        acceptance=_acceptance(event, release, reproduction),
        repo=repository or str(root),
        ref=commit,
        impacted_paths=list(diagnosis.suspected_paths),
    )
    store = WorkItems(trace, now=now)
    issue = event.short_id or event.id or event.title
    item = store.add(
        ticket,
        actor=ACTOR,
        pipeline_run_id=run_id,
        reason=f"filed from Sentry issue {issue} at release {release}",
    )
    if reproduction.reproduced:
        item = store.move(
            item.id,
            "ready",
            f"{', '.join(reproduction.test_files)} reproduces the event at {release}",
            actor=ACTOR,
            pipeline_run_id=run_id,
        )
    item = store.link_run(item.id, run_id)
    result = IncidentResult(
        pipeline_run_id=run_id,
        repo_path=str(root),
        release=release,
        commit=commit,
        event=event,
        triage=agents.triage,
        diagnosis=diagnosis,
        test_writer=agents.test_writer,
        test_writer_output=written,
        reproduction=reproduction,
        confidence=confidence,
        item=item,
        report_path=out / f"{run_id}.json",
        total_cost_usd=trace.cost_by_pipeline_run(run_id),
        duration_ms=round((time.monotonic() - start) * 1000),
    )
    result.report_path.write_text(json.dumps(result.to_json(), indent=2) + "\n", encoding="utf-8")
    return result


def command(
    repo_path: str,
    *,
    event: str,
    ref: str | None = None,
    repository: str | None = None,
    out_dir: str | None = None,
    work_dir: str | None = None,
    budget_usd: float | None = None,
    as_json: bool = False,
    allow_unpublished: bool = False,
) -> int:
    """Run `factory incident`: diagnose one Sentry event in `repo_path` and file its ticket.

    `event` is the issue's JSON or `@path` of a file holding it. It and the release are checked
    before a model is called. Prose goes to stderr under `as_json`, where stdout gets exactly one
    JSON document, the report. Without it the text report goes to stdout. Returns 0 whenever a
    ticket is filed, reproduced or not; the ticket's status says which. A missing key is
    `AuthOrConfigError` (exit 4).
    """
    document = load_event(event)
    agents = resolve_incident_agents(allow_unpublished=allow_unpublished)
    parsed = parse_event(agents.triage, document)
    release_commit(resolve_root(repo_path), parsed, ref)
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(
            f"{API_KEY_ENV} is not set",
            hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
        )
    budget = Budget(DEFAULT_BUDGET_USD if budget_usd is None else budget_usd)
    with Trace() as trace:
        result = run_incident(
            agents,
            repo_path,
            parsed,
            trace,
            ref=ref,
            repository=repository,
            out_dir=DEFAULT_OUT_DIR if out_dir is None else out_dir,
            work_dir=work_dir,
            runner=run,
            budget=budget,
        )
    if as_json:
        print(result.text(), file=sys.stderr)
        print(json.dumps(result.to_json()))
    else:
        print(result.text())
    return ExitCode.OK


__all__: Sequence[str] = [
    "ACTOR",
    "BODY_SECTIONS",
    "DEFAULT_OUT_DIR",
    "REPRODUCED_CONFIDENCE",
    "TEST_WRITER",
    "TRIAGE",
    "IncidentAgents",
    "IncidentError",
    "IncidentResult",
    "Reproduction",
    "command",
    "crash_file",
    "incident_sections",
    "is_reproduced",
    "load_event",
    "parse_event",
    "release_commit",
    "resolve_incident_agents",
    "run_incident",
    "stack_trace",
    "ticket_body",
]
