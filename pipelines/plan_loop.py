"""The plan loop: from an idea to tickets in the store, behind a human approval (#92).

`resolve_plan_agents` pins the two agents before anything runs, `run_plan` does one invocation and
`command` is what `factory plan` calls. One plan takes two invocations, or one when the PRD has no
open question and `--approve` is given at once:

1. `product-architect` writes the PRD from the idea. It is printed and stored as
   `<plans dir>/<run id>.md`, a Markdown file a person can read and edit. Nothing else happens.
2. A person answers each open question, on the command line with `--answer` or by writing it
   after `A:` in the file, and approves with `--approve` on a second invocation that gives the
   file back as `--prd @file`. The second invocation keeps the run id of the first.
3. `story-breakdown` turns the approved PRD into tickets. They are written to the work-item store
   in `todo`, parent `prd:<run id>`, dependencies set, all or none (`WorkItems.add_many`).

The loop never moves an item to `ready`: `factory ticket ready` does, from a person's hands or the
orchestrator's. Every agent run and every store event of one plan carries the same pipeline run
id, and the plan's cost is not linked to the tickets, so a ticket's cost stays its own.
"""

import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from factory.exit import AuthOrConfigError, ExitCode, FactoryError, NotFoundError, UsageError
from pipelines.ids import new_pipeline_run_id
from registry.resolver import resolve
from registry.spec import LLMSpec
from runtime.budget import DEFAULT_BUDGET_USD, Budget
from runtime.runner import API_KEY_ENV, run
from runtime.trace import Trace
from runtime.work_items import NewItem, WorkItem, WorkItems
from tools._common import resolve_commit

Runner = Callable[..., BaseModel]

ARCHITECT: tuple[str, str] = ("product-architect", "^0.1")
"""The agent that writes the PRD and the version range it is pinned to."""

BREAKDOWN: tuple[str, str] = ("story-breakdown", "^0.1")
"""The agent that breaks the approved PRD into tickets and the version range it is pinned to."""

ACTOR = "pipeline:plan_loop"
"""The actor of the creation events: ADR 0002's `pipeline:<module>`."""

DEFAULT_PLANS_DIR = Path("data/plans")
"""Where the PRDs go, relative to the working directory like the trace database."""

type Status = str
AWAITING_ANSWERS = "awaiting_answers"
AWAITING_APPROVAL = "awaiting_approval"
FILED = "filed"

SERIALIZED = "serialized"
UNKNOWN_PATHS = "unknown_paths"


class PlanError(FactoryError):
    """The plan could not go on: the PRD is already filed, or the file is not a plan (exit 1)."""


@dataclass(frozen=True, slots=True)
class PlanAgents:
    """The resolved agents of one plan: the architect and the breakdown."""

    architect: LLMSpec
    breakdown: LLMSpec


def resolve_plan_agents(
    *, allow_unpublished: bool = False, trace_path: Path | None = None
) -> PlanAgents:
    """Resolve `product-architect` and `story-breakdown`; unless `allow_unpublished`, published ones.

    Raises `UsageError` when either is not an LLM agent, and the resolver's `NotFoundError`
    subclasses (exit 3) when an agent or version is missing.
    """
    found = []
    for name, version_range in (ARCHITECT, BREAKDOWN):
        spec = resolve(
            name, version_range, published_only=not allow_unpublished, trace_path=trace_path
        )
        if not isinstance(spec, LLMSpec):
            raise UsageError(f"{spec.address} is not an LLM agent, so the plan loop cannot run it")
        found.append(spec)
    return PlanAgents(architect=found[0], breakdown=found[1])


# --- the PRD file -------------------------------------------------------------------------------

_HEADER_KEYS = ("plan_run_id", "repository", "ref", "idea", "status", "tickets")
_QUESTION = "## Open questions"
_SECTION = re.compile(r"^## ")


@dataclass(slots=True)
class Question:
    """One open question of a PRD, with the answer a person gave it, or None."""

    question: str
    why_it_matters: str
    answer: str | None = None


@dataclass(slots=True)
class Prd:
    """A PRD file: the header the plan loop keeps, the text, and the questions parsed from it.

    `text` is everything after the header with the open-questions section left out, so
    `render()` puts the section back from `questions`.
    """

    plan_run_id: str
    repository: str
    ref: str
    idea: str
    text: str
    questions: list[Question]
    status: str = "draft"
    tickets: list[str] = field(default_factory=list)

    def unanswered(self) -> list[Question]:
        return [q for q in self.questions if not (q.answer or "").strip()]

    def render(self) -> str:
        """The file's text: header, the PRD and the open-questions section."""
        header = [f"{key}: {json.dumps(getattr(self, key))}" for key in _HEADER_KEYS]
        lines = ["---", *header, "---", ""]
        body = self.text.rstrip()
        if self.questions:
            body += "\n\n" + self._questions_section()
        return "\n".join(lines) + body + "\n"

    def approved_text(self) -> str:
        """The PRD as `story-breakdown` reads it: the text and the questions with their answers."""
        body = self.text.rstrip()
        if self.questions:
            body += "\n\n" + self._questions_section()
        return body + "\n"

    def _questions_section(self) -> str:
        lines = [_QUESTION, ""]
        for number, q in enumerate(self.questions, start=1):
            lines.append(f"- Q{number}: {_one_line(q.question)}")
            if q.why_it_matters.strip():
                lines.append(f"  Why: {_one_line(q.why_it_matters)}")
            answer = (q.answer or "").strip().splitlines()
            lines.append(f"  A: {answer[0] if answer else ''}".rstrip())
            lines.extend(f"    {more}" for more in answer[1:])
        return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {_one_line(item)}" for item in items) if items else "None."


def prd_text(output: BaseModel) -> str:
    """Render a `PRDOutput` as the Markdown body of the PRD, without the open questions."""
    data = output.model_dump()
    stories = [
        f"As {s['as_a']}, I want {s['i_want']}, so that {s['so_that']}."
        for s in data["user_stories"]
    ]
    return "\n\n".join(
        [
            "# PRD",
            f"## Problem\n\n{data['problem'].strip()}",
            f"## Goals\n\n{_bullets(data['goals'])}",
            f"## Non-goals\n\n{_bullets(data['non_goals'])}",
            f"## User stories\n\n{_bullets(stories)}",
            f"## Constraints\n\n{_bullets(data['constraints'])}",
            f"## Risks\n\n{_bullets(data['risks'])}",
            f"## Related files\n\n{_bullets(data['related_files'])}",
        ]
    )


def _questions_of(output: BaseModel) -> list[Question]:
    return [
        Question(question=_one_line(q["question"]), why_it_matters=_one_line(q["why_it_matters"]))
        for q in output.model_dump()["open_questions"]
    ]


def parse_prd(source: str) -> Prd:
    """Parse a PRD file as `Prd.render` writes it.

    A person may edit the text and write answers after `A:`; the header keys must stay. Raises
    `PlanError` for a file that has no header or lacks a key, and a question without a `Q<n>:`
    line it can read.
    """
    lines = source.splitlines()
    if not lines or lines[0].strip() != "---" or "---" not in [x.strip() for x in lines[1:]]:
        raise PlanError("not a plan file: it has no header", hint="plan files start with `---`")
    end = [x.strip() for x in lines].index("---", 1)
    header: dict[str, Any] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        try:
            header[key.strip()] = json.loads(value)
        except ValueError:
            raise PlanError(f"plan file header line {line!r} is not `key: <JSON>`") from None
    missing = [k for k in _HEADER_KEYS if k not in header]
    if missing:
        raise PlanError(f"plan file header lacks {', '.join(missing)}")
    body = lines[end + 1 :]
    text_lines: list[str] = []
    section: list[str] | None = None
    for line in body:
        if line.strip() == _QUESTION:
            section = []
        elif section is not None and _SECTION.match(line):
            text_lines.extend(["", line])
            section = None
        elif section is not None:
            section.append(line)
        else:
            text_lines.append(line)
    return Prd(
        plan_run_id=str(header["plan_run_id"]),
        repository=str(header["repository"]),
        ref=str(header["ref"]),
        idea=str(header["idea"]),
        status=str(header["status"]),
        tickets=[str(t) for t in header["tickets"]],
        text="\n".join(text_lines).strip("\n"),
        questions=_parse_questions(section or []),
    )


_Q_LINE = re.compile(r"^- Q\d+: (.+)$")


def _parse_questions(lines: Sequence[str]) -> list[Question]:
    questions: list[Question] = []
    answer: list[str] | None = None
    for line in lines:
        if match := _Q_LINE.match(line):
            questions.append(Question(question=match.group(1).strip(), why_it_matters=""))
            answer = None
        elif not line.strip():
            continue
        elif not questions:
            raise PlanError(f"open question line {line!r} is not `- Q<n>: <question>`")
        elif line.startswith("  Why:"):
            questions[-1].why_it_matters = line[len("  Why:") :].strip()
        elif line.startswith("  A:"):
            answer = [line[len("  A:") :].strip()]
            questions[-1].answer = answer[0] or None
        elif line.startswith("    ") and answer is not None:
            answer.append(line.strip())
            questions[-1].answer = "\n".join(answer)
        else:
            raise PlanError(f"open question line {line!r} is not `  Why:` or `  A:`")
    return questions


# --- answers ------------------------------------------------------------------------------------


def parse_answer(text: str, questions: Sequence[Question]) -> tuple[int, str]:
    """Match `--answer "<question>=<answer>"` to a question; return its index and the answer.

    The question is named by its text, compared without regard to case or runs of spaces, or by
    its number (`1` or `Q1`) in the PRD. The text may itself hold an `=`: the first `=` that leaves
    the name of a question on its left decides. Raises `UsageError` for an answer that names no
    question, one that is empty, and when the PRD has no open question.
    """
    if not questions:
        raise UsageError("the PRD has no open question to answer", hint=f"drop --answer {text!r}")
    for position, char in enumerate(text):
        if char != "=":
            continue
        name = " ".join(text[:position].split())
        answer = text[position + 1 :].strip()
        index = _match(name, questions)
        if index is None:
            continue
        if not answer:
            raise UsageError(f"the answer to {questions[index].question!r} is empty")
        return index, answer
    known = "; ".join(f"Q{n}: {q.question}" for n, q in enumerate(questions, start=1))
    raise UsageError(
        f"--answer {text!r} matches no open question",
        hint=f'write --answer "<question or number>=<answer>"; the questions are {known}',
    )


def _match(name: str, questions: Sequence[Question]) -> int | None:
    folded = name.casefold()
    for index, q in enumerate(questions):
        if _one_line(q.question).casefold() == folded:
            return index
    number = re.fullmatch(r"[Qq]?([1-9][0-9]*)", name)
    if number and int(number.group(1)) <= len(questions):
        return int(number.group(1)) - 1
    return None


def apply_answers(prd: Prd, answers: Sequence[str]) -> list[int]:
    """Record each `--answer` in `prd`, and return the indexes it answered.

    An answer replaces what the file already holds for that question. Two `--answer` for one
    question in one invocation are a `UsageError`, since the later one would silently win.
    """
    done: list[int] = []
    for text in answers:
        index, answer = parse_answer(text, prd.questions)
        if index in done:
            raise UsageError(f"question {index + 1} is answered twice in this command")
        prd.questions[index].answer = answer
        done.append(index)
    return done


# --- tickets ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two planned tickets that could run together and might touch the same files."""

    earlier: int
    later: int
    paths: tuple[str, ...]
    resolution: str

    def to_json(self) -> dict[str, Any]:
        return {
            "earlier": self.earlier,
            "later": self.later,
            "paths": list(self.paths),
            "resolution": self.resolution,
        }


def plan_batch(breakdown: Any) -> tuple[list[NewItem], list[Conflict]]:
    """Turn a `StoryOutput` into the batch for `WorkItems.add_many` and the conflicts it found.

    A pair that `StoryOutput.parallel_conflicts()` reports with shared paths is made sequential:
    the later ticket depends on the earlier one, unless it already follows it through others,
    and the pair is reported as `serialized`. Pairs are handled by later ticket, nearest earlier
    ticket first, so a run of tickets on one file becomes a chain, not a fan. A
    pair with no paths to compare on one side or both is reported as `unknown_paths` and left
    parallel, since an empty `impacted_paths` says "unknown", not "the same file". The plan is
    never refused for a conflict. The later ticket always depends on the earlier, so the added
    edges follow the breakdown's build order and cannot close a cycle.
    """
    tickets = breakdown.tickets
    deps: list[list[int]] = [list(t.depends_on) for t in tickets]
    found: list[Conflict] = []
    for earlier, later, shared in sorted(
        breakdown.parallel_conflicts(),
        key=lambda c: (c[1], -c[0]),
    ):
        if not shared:
            found.append(Conflict(earlier, later, (), UNKNOWN_PATHS))
            continue
        if earlier not in _reach(deps, later):
            deps[later].append(earlier)
        found.append(Conflict(earlier, later, tuple(shared), SERIALIZED))
    found.sort(key=lambda c: (c.earlier, c.later))
    batch = [
        NewItem(ticket.as_ticket(), ticket.size, tuple(sorted(deps[n])))
        for n, ticket in enumerate(tickets)
    ]
    return batch, found


def _reach(deps: Sequence[Sequence[int]], index: int) -> set[int]:
    seen: set[int] = set()
    stack = list(deps[index])
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(deps[node])
    return seen


# --- one invocation -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanResult:
    """What one `factory plan` invocation did and where the plan stands."""

    pipeline_run_id: str
    status: Status
    approve_requested: bool
    prd_path: Path
    prd: Prd
    cost_usd: float
    duration_ms: int
    summary: str | None = None
    items: tuple[WorkItem, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    breakdown_questions: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        """0 unless approval was asked for and the plan did not go through."""
        return ExitCode.ERROR if self.approve_requested and self.status != FILED else ExitCode.OK

    def to_json(self) -> dict[str, Any]:
        return {
            "pipeline_run_id": self.pipeline_run_id,
            "status": self.status,
            "approved": self.status == FILED,
            "prd_path": str(self.prd_path),
            "repository": self.prd.repository,
            "ref": self.prd.ref,
            "open_questions": [
                {"question": q.question, "why_it_matters": q.why_it_matters, "answer": q.answer}
                for q in self.prd.questions
            ],
            "unanswered": len(self.prd.unanswered()),
            "summary": self.summary,
            "tickets": [
                {
                    "id": item.id,
                    "title": item.title,
                    "size": item.size,
                    "status": item.status,
                    "depends_on": list(item.depends_on),
                    "impacted_paths": list(item.impacted_paths),
                }
                for item in self.items
            ],
            "conflicts": [
                {
                    **c.to_json(),
                    "earlier": self.items[c.earlier].id if self.items else c.earlier,
                    "later": self.items[c.later].id if self.items else c.later,
                }
                for c in self.conflicts
            ],
            "breakdown_open_questions": list(self.breakdown_questions),
            "cost_usd": round(self.cost_usd, 6),
            "duration_ms": self.duration_ms,
        }

    def text(self) -> str:
        """The report: the PRD as stored, then where the plan stands and what to do next."""
        lines = [self.prd.approved_text().rstrip(), "", f"PRD: {self.prd_path}"]
        lines.append(f"Plan run: {self.pipeline_run_id}")
        if self.status == AWAITING_ANSWERS:
            lines.append(
                f"Not approved: {len(self.prd.unanswered())} open question(s) have no answer."
            )
            lines.append(
                f'Answer with --answer "<question or number>=<answer>" (or write after `A:` in '
                f"the file), then run: factory plan <repo> --prd @{self.prd_path} --approve"
            )
        elif self.status == AWAITING_APPROVAL:
            lines.append(
                f"Awaiting approval. Read the PRD, edit {self.prd_path} if needed, then run: "
                f"factory plan <repo> --prd @{self.prd_path} --approve"
            )
        else:
            lines.append(
                f"Filed {len(self.items)} ticket(s) in todo, parent prd:{self.pipeline_run_id}:"
            )
            for n, item in enumerate(self.items):
                after = f" (after {', '.join(item.depends_on)})" if item.depends_on else ""
                lines.append(f"  {item.id} [{item.size or '-'}] {item.title}{after}")
            for c in self.conflicts:
                first, second = self.items[c.earlier].id, self.items[c.later].id
                if c.resolution == SERIALIZED:
                    lines.append(f"  {second} runs after {first}: both touch {', '.join(c.paths)}")
                else:
                    lines.append(
                        f"  {first} and {second} may run together; one has no impacted paths"
                    )
            for question in self.breakdown_questions:
                lines.append(f"  Assumed by the breakdown: {question}")
            lines.append("Nothing is ready: `factory ticket ready` promotes the roots.")
        return "\n".join(lines)


def run_plan(
    agents: PlanAgents,
    repo_path: str | Path,
    trace: Trace,
    *,
    idea: str | None = None,
    prd_source: str | None = None,
    answers: Sequence[str] = (),
    approve: bool = False,
    ref: str = "HEAD",
    repository: str | None = None,
    plans_dir: str | Path = DEFAULT_PLANS_DIR,
    runner: Runner = run,
    budget: Budget | None = None,
) -> PlanResult:
    """Run one invocation of the plan loop and return where the plan stands.

    Give `idea` for a new plan or `prd_source` (the text of a stored PRD file) to carry one on,
    never both. A new plan writes the PRD with `agents.architect` and stores it; `ref` is
    resolved to a commit in `repo_path` and `repository` defaults to its absolute path. A plan
    carried on keeps its run id, repository and ref from the file, applies `answers` and stores
    the file again under `plans_dir`.

    `approve` files tickets only when every open question has an answer, then runs
    `agents.breakdown` on the PRD with the answers in it and writes the tickets and their events
    in one transaction. Otherwise nothing but the PRD file is written, and the result says
    `awaiting_answers` or `awaiting_approval`. Raises `UsageError` for a bad combination or
    `--answer`, `PlanError` for a PRD that is filed already or not a plan file, and what the
    runner raises for a failed agent run, with the PRD file already stored.
    """
    if (idea is None) == (prd_source is None):
        raise UsageError("give either an idea or a PRD file")
    if idea is not None and answers:
        raise UsageError(
            "a new PRD has no questions to answer yet", hint="answer them with --prd @file"
        )
    started = time.monotonic()
    plans = Path(plans_dir)
    store = WorkItems(trace)
    if idea is not None:
        prd = _write_prd(agents, repo_path, trace, idea, ref, repository, runner, budget)
    else:
        assert prd_source is not None
        prd = parse_prd(prd_source)
        _refuse_filed(prd, store)
        apply_answers(prd, answers)
    path = plans / f"{prd.plan_run_id}.md"
    _store(path, prd)

    def result(status: Status, **more: Any) -> PlanResult:
        return PlanResult(
            pipeline_run_id=prd.plan_run_id,
            status=status,
            approve_requested=approve,
            prd_path=path,
            prd=prd,
            cost_usd=trace.cost_by_pipeline_run(prd.plan_run_id),
            duration_ms=int((time.monotonic() - started) * 1000),
            **more,
        )

    if prd.unanswered():
        return result(AWAITING_ANSWERS)
    if not approve:
        return result(AWAITING_APPROVAL)
    story: Any = runner(
        agents.breakdown,
        {
            "repo_path": str(repo_path),
            "prd": prd.approved_text(),
            "repository": prd.repository,
            "ref": prd.ref,
        },
        trace,
        repo_path=repo_path,
        pipeline_run_id=prd.plan_run_id,
        budget=budget,
    )
    batch, conflicts = plan_batch(story)
    items = store.add_many(
        batch,
        parent=f"prd:{prd.plan_run_id}",
        actor=ACTOR,
        pipeline_run_id=prd.plan_run_id,
        reason=f"planned from the approved PRD {prd.plan_run_id}",
    )
    prd.status = FILED
    prd.tickets = [item.id for item in items]
    breakdown_questions = tuple(story.open_questions)
    prd.text = _with_assumptions(prd.text, breakdown_questions)
    _store(path, prd)
    return result(
        FILED,
        summary=story.summary,
        items=tuple(items),
        conflicts=tuple(conflicts),
        breakdown_questions=breakdown_questions,
    )


def _write_prd(
    agents: PlanAgents,
    repo_path: str | Path,
    trace: Trace,
    idea: str,
    ref: str,
    repository: str | None,
    runner: Runner,
    budget: Budget | None,
) -> Prd:
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise NotFoundError(f"no repository at {repo_path}")
    commit = resolve_commit(root, ref)
    run_id = new_pipeline_run_id()
    output = runner(
        agents.architect,
        {"repo_path": str(root), "ref": commit, "idea": idea, "feedback": []},
        trace,
        repo_path=root,
        pipeline_run_id=run_id,
        budget=budget,
    )
    return Prd(
        plan_run_id=run_id,
        repository=repository or str(root),
        ref=commit,
        idea=idea,
        text=prd_text(output),
        questions=_questions_of(output),
    )


def _refuse_filed(prd: Prd, store: WorkItems) -> None:
    parent = f"prd:{prd.plan_run_id}"
    filed = [item.id for item in store.list() if item.parent == parent]
    if prd.status == FILED or filed:
        raise PlanError(
            f"PRD {prd.plan_run_id} is already filed as {', '.join(filed or prd.tickets)}",
            hint="start a new plan with --idea; `factory ticket list` shows the tickets",
        )


def _store(path: Path, prd: Prd) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(prd.render(), encoding="utf-8")
    temporary.replace(path)


def _with_assumptions(text: str, questions: Sequence[str]) -> str:
    if not questions:
        return text
    return text.rstrip() + "\n\n## Breakdown assumptions\n\n" + _bullets(questions)


# --- the command --------------------------------------------------------------------------------


def _read(value: str, flag: str) -> str:
    """Return `value`, or the text of the file when it is `@path`."""
    if not value.startswith("@"):
        return value
    path = Path(value[1:])
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFoundError(f"{flag}: no file at {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"{flag}: cannot read {path}: {exc}") from None


def command(
    repo_path: str,
    *,
    idea: str | None = None,
    prd: str | None = None,
    answers: Sequence[str] = (),
    approve: bool = False,
    ref: str = "HEAD",
    repository: str | None = None,
    plans_dir: str | None = None,
    budget_usd: float | None = None,
    as_json: bool = False,
    allow_unpublished: bool = False,
) -> int:
    """Run `factory plan`: write a PRD from an idea, or carry a stored one on.

    `idea` is the text or `@file`; `prd` is `@file`, the PRD file a first invocation stored.
    Exactly one of the two is given, `ref` and `repository` go with `idea` only, and `answers`
    with `prd` only; any other combination is a `UsageError` (exit 2) before a model is called.
    Prose goes to stderr under `as_json`, where stdout gets exactly one JSON document; without it
    the text report goes to stdout. Returns 0 when the PRD is stored (and, with `approve`, the
    tickets filed) and 1 when `approve` was given and the plan did not go through. A missing key
    is `AuthOrConfigError` (exit 4).
    """
    if (idea is None) == (prd is None):
        raise UsageError("give --idea or --prd, one of them", hint="a new plan takes --idea")
    if prd is not None and (ref != "HEAD" or repository is not None):
        raise UsageError("--ref and --repository belong to --idea; the PRD file holds them")
    if idea is not None and answers:
        raise UsageError("--answer needs --prd: a new PRD has no questions to answer yet")
    if prd is not None and not prd.startswith("@"):
        raise UsageError("--prd takes @FILE, the PRD file a first invocation stored")
    idea_text = _read(idea, "--idea") if idea is not None else None
    if idea_text is not None and not idea_text.strip():
        raise UsageError("the idea is empty")
    prd_source = _read(prd, "--prd") if prd is not None else None
    agents = resolve_plan_agents(allow_unpublished=allow_unpublished)
    if (idea_text is not None or approve) and not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(
            f"{API_KEY_ENV} is not set",
            hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
        )
    budget = Budget(DEFAULT_BUDGET_USD if budget_usd is None else budget_usd)
    with Trace() as trace:
        result = run_plan(
            agents,
            repo_path,
            trace,
            idea=idea_text,
            prd_source=prd_source,
            answers=answers,
            approve=approve,
            ref=ref,
            repository=repository,
            plans_dir=DEFAULT_PLANS_DIR if plans_dir is None else plans_dir,
            runner=run,
            budget=budget,
        )
    if as_json:
        print(result.text(), file=sys.stderr)
        print(json.dumps(result.to_json()))
    else:
        print(result.text())
    return result.exit_code


__all__ = [
    "ACTOR",
    "ARCHITECT",
    "AWAITING_ANSWERS",
    "AWAITING_APPROVAL",
    "BREAKDOWN",
    "FILED",
    "Conflict",
    "PlanAgents",
    "PlanError",
    "PlanResult",
    "Prd",
    "Question",
    "apply_answers",
    "command",
    "parse_answer",
    "parse_prd",
    "plan_batch",
    "prd_text",
    "resolve_plan_agents",
    "run_plan",
]
