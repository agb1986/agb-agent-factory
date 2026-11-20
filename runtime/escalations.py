"""Escalation records: the question a stopped pipeline asks a person, and the answer (#93).

A pipeline that stops on something a person can settle calls `escalate`. One call writes one row
of `escalations` (migration 6 in `runtime/trace.py`) and moves the work item to `escalated`, in
one transaction under the trace's single writer, through `WorkItems.move_with`: either both
happen or neither does. The row is a frozen picture of the stop: the question, the item's state
before the move, its pipeline runs, the cost so far, the workspace diff, the last agent outputs
and the tail of the suite output. Large fields are capped (see `DIFF_CHARS` and the others), and
the cut is marked in the text, so a record never grows without bound.

Nothing here reads the environment. A record holds what the caller passes and what the store
already has; a caller must never pass a token, a key or an environment dump.

`answer` stores a person's answer and, when it closes the item's last open record, moves the item
from `escalated` to `ready` in the same transaction. The next run sees the answers through
`ticket_for_run`, which renders them into the ticket body, so no agent's input schema changes.
`render_markdown` and `answer` are plain functions so the summary agent (#94) and the issue
notifier (#95) can reuse them.

A record may carry one summary (#94): a row of `escalation_summaries` (migration 7) holding the
escalation-summary agent's output and the run that wrote it. `store_summary` writes it once,
`get_summary` reads it, and `render_markdown` prints it above the record. The summary is written
by `runtime.after_escalation`, never by `escalate`, so a record never waits on a model. The rules
are in `docs/CONVENTIONS.md` (Escalations).
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, get_args

import duckdb

from factory.exit import NotFoundError, UsageError
from registry.common_schemas import Ticket
from runtime.trace import Trace
from runtime.work_items import WorkItem, WorkItems

Trigger = Literal["blocked_output", "budget", "changes_requested", "contradiction", "check_failed"]

TRIGGERS: tuple[Trigger, ...] = get_args(Trigger)
"""Every reason a pipeline may escalate, in the order `docs/CONVENTIONS.md` lists them."""

QUESTION_CHARS = 4000
"""The longest question kept. A longer one keeps its head."""

DIFF_CHARS = 20000
"""The longest workspace diff kept. A longer one keeps its head, where the first files are."""

OUTPUT_CHARS = 8000
"""The longest text kept for each agent output. A longer one keeps its head."""

SUITE_TAIL_CHARS = 4000
"""The longest suite output kept. A longer one keeps its end, where the failures are summed up."""

ANSWER_CHARS = 8000
"""The longest answer accepted. A longer one is refused, not cut: a person's words stay whole."""

ANSWERS_HEADING = "## Answers to earlier escalations"
"""The heading `with_answers` puts above the answers in the ticket body."""

_ID = re.compile(r"esc-([1-9][0-9]*)")
_COLUMNS = (
    "id, work_item_id, timezone('UTC', created_at), actor, pipeline_run_id, \"trigger\","
    " question, from_status, pipeline_run_ids, cost_usd, diff, agent_outputs, suite_tail, answer,"
    " answer_source, timezone('UTC', answered_at)"
)


class EscalationNotFound(NotFoundError):
    """No escalation has this id. Exit 3."""


class InvalidEscalation(UsageError):
    """A bad trigger, an empty question or answer, or an answer the record cannot take. Exit 2."""


@dataclass(frozen=True)
class AgentOutput:
    """One agent's last output as a record keeps it: the agent, its run and the output as text."""

    agent: str
    run_id: str | None
    output: str

    @classmethod
    def of(cls, agent: str, run_id: str | None, output: object) -> AgentOutput:
        """Return the output with `output` as text: a string as it is, anything else as JSON."""
        text = output if isinstance(output, str) else json.dumps(output, indent=2, default=str)
        return cls(agent, run_id, text)

    def to_json(self) -> dict[str, Any]:
        """Return the output as the JSON object `factory escalations show --json` prints."""
        return {"agent": self.agent, "run_id": self.run_id, "output": self.output}


@dataclass(frozen=True)
class Escalation:
    """One row of `escalations`. `answer` is None while the record is open."""

    id: str
    work_item_id: str
    created_at: datetime
    actor: str
    pipeline_run_id: str | None
    trigger: Trigger
    question: str
    from_status: str
    pipeline_run_ids: tuple[str, ...]
    cost_usd: float
    diff: str
    agent_outputs: tuple[AgentOutput, ...]
    suite_tail: str
    answer: str | None
    answer_source: str | None
    answered_at: datetime | None

    @property
    def is_open(self) -> bool:
        """True until the record is answered."""
        return self.answer is None

    def to_json(self) -> dict[str, Any]:
        """Return every column as a JSON object, timestamps as ISO 8601 UTC with a `Z`."""
        return {
            "id": self.id,
            "work_item_id": self.work_item_id,
            "created_at": _iso(self.created_at),
            "actor": self.actor,
            "pipeline_run_id": self.pipeline_run_id,
            "trigger": self.trigger,
            "question": self.question,
            "from_status": self.from_status,
            "pipeline_run_ids": list(self.pipeline_run_ids),
            "cost_usd": self.cost_usd,
            "diff": self.diff,
            "agent_outputs": [output.to_json() for output in self.agent_outputs],
            "suite_tail": self.suite_tail,
            "open": self.is_open,
            "answer": self.answer,
            "answer_source": self.answer_source,
            "answered_at": _iso(self.answered_at) if self.answered_at else None,
        }


@dataclass(frozen=True)
class Summary:
    """One row of `escalation_summaries`: the summary of one record and the run that wrote it.

    `summary` is the escalation-summary agent's output as a JSON object: `paragraph` and
    `options`, each option with `label`, `takes`, `cost` and `evidence`.
    """

    escalation_id: str
    run_id: str
    agent_ref: str
    summary: Mapping[str, Any]
    created_at: datetime

    def to_json(self) -> dict[str, Any]:
        """Return the row as the object `factory escalations show --json` prints as `summary`."""
        return {
            "escalation_id": self.escalation_id,
            "run_id": self.run_id,
            "agent_ref": self.agent_ref,
            "paragraph": self.summary.get("paragraph", ""),
            "options": list(self.summary.get("options", [])),
            "created_at": _iso(self.created_at),
        }


def escalate(
    trace: Trace,
    item_id: str,
    *,
    trigger: Trigger,
    question: str,
    reason: str,
    actor: str,
    pipeline_run_id: str | None,
    diff: str = "",
    agent_outputs: Sequence[AgentOutput] = (),
    suite_tail: str = "",
    now: Callable[[], datetime] | None = None,
) -> Escalation:
    """Write one escalation record and move the item to `escalated`, atomically. Return the record.

    The id is `esc-<n>`, one more than the highest in the store. `question` is what the person is
    asked; `reason` is the move's event reason. The record takes the item's state before the move,
    its linked pipeline runs and its cost so far as a snapshot from the trace. `diff`, each output
    and `suite_tail` are capped as the module constants say, with a marker where the cut is.

    Raises `InvalidEscalation` for an unknown trigger or an empty question, and whatever
    `WorkItems.move_with` raises: `WorkItemNotFound` for an unknown item, `IllegalTransitionError`
    when the item is in a state that cannot move to `escalated` (only `in_progress` and `blocked`
    can), and `InvalidWorkItem` for a bad actor or reason. On any error nothing is written.
    """
    if trigger not in TRIGGERS:
        raise InvalidEscalation(
            f"{trigger!r} is not a trigger", hint=f"one of {', '.join(TRIGGERS)}"
        )
    question = question.strip()
    if not question:
        raise InvalidEscalation(
            "an escalation needs a question", hint="say what a person must decide"
        )
    items = WorkItems(trace, now=now)
    stamp = _stamp(now)
    outputs = tuple(
        AgentOutput(o.agent, o.run_id, cut_head(o.output, OUTPUT_CHARS)) for o in agent_outputs
    )
    written: list[str] = []

    def write(con: duckdb.DuckDBPyConnection, item: WorkItem) -> None:
        esc_id = f"esc-{_max_number(con) + 1}"
        con.execute(
            "INSERT INTO escalations VALUES"
            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            [
                esc_id,
                item.id,
                stamp,
                actor,
                pipeline_run_id,
                trigger,
                cut_head(question, QUESTION_CHARS),
                item.status,
                json.dumps(list(item.pipeline_run_ids)),
                items.cost_usd(item.id),
                cut_head(diff, DIFF_CHARS),
                json.dumps([o.to_json() for o in outputs]),
                cut_tail(suite_tail, SUITE_TAIL_CHARS),
            ],
        )
        written.append(esc_id)

    items.move_with(
        item_id, "escalated", reason, actor=actor, pipeline_run_id=pipeline_run_id, write=write
    )
    return get(trace, written[0])


def get(trace: Trace, escalation_id: str) -> Escalation:
    """Return one record. Raises `EscalationNotFound` for an unknown or malformed id."""
    with trace.connection() as con:
        return _get(con, escalation_id)


def list_escalations(
    trace: Trace, *, work_item_id: str | None = None, open_only: bool = False
) -> list[Escalation]:
    """Return the records, oldest first, of one item or of all, and only the open ones if asked.

    An unknown item has no records, so it returns an empty list rather than raising.
    """
    where, args = [], []
    if work_item_id is not None:
        where.append("work_item_id = ?")
        args.append(work_item_id)
    if open_only:
        where.append("answer IS NULL")
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with trace.connection() as con:
        rows = con.execute(f"SELECT {_COLUMNS} FROM escalations{clause}", args).fetchall()
    return sorted((_record(row) for row in rows), key=lambda r: _number(r.id))


def answer(
    trace: Trace,
    escalation_id: str,
    text: str,
    source: str,
    *,
    now: Callable[[], datetime] | None = None,
) -> Escalation:
    """Store a person's answer on an open record and return the answered record.

    `source` says where the answer came from, such as `cli` or `file:<path>`, for the record.
    When this answer closes the item's last open record, the item moves from `escalated` to
    `ready` as actor `human`, in the same transaction as the answer; while other records on the
    item are still open, only the answer is stored and the item stays `escalated`.

    Raises `EscalationNotFound` for an unknown id. Raises `InvalidEscalation`, and stores nothing,
    for an empty answer or one over `ANSWER_CHARS`, an empty source, a record already answered
    (an answer is never replaced), or an item that is no longer `escalated` (a person moved it on
    by hand, so the question no longer stands). Raises `IllegalTransitionError` when the item
    cannot be `ready` because a dependency is no longer `done`; nothing is stored then either.
    """
    text = text.strip()
    if not text:
        raise InvalidEscalation("the answer is empty", hint="pass --text TEXT or --text @file")
    if len(text) > ANSWER_CHARS:
        raise InvalidEscalation(
            f"the answer is {len(text)} characters, over the limit of {ANSWER_CHARS}",
            hint="shorten it, or link to a longer note",
        )
    if not source.strip():
        raise InvalidEscalation("an answer needs a source", hint="such as cli or file:<path>")
    stamp = _stamp(now)
    items = WorkItems(trace, now=now)
    with trace.connection() as con:
        record = _get(con, escalation_id)
        if not record.is_open:
            raise InvalidEscalation(
                f"{record.id} is already answered",
                hint=f"factory escalations show {record.id} shows the answer",
            )

        def store(con: duckdb.DuckDBPyConnection, item: WorkItem) -> None:
            if item.status != "escalated":
                raise InvalidEscalation(
                    f"{item.id} is {item.status}, not escalated, so {record.id} no longer stands",
                    hint=f"factory ticket show {item.id} shows how it moved",
                )
            con.execute(
                "UPDATE escalations SET answer = ?, answer_source = ?, answered_at = ?"
                " WHERE id = ? AND answer IS NULL",
                [text, source, stamp, record.id],
            )

        others = [
            r
            for r in list_escalations(trace, work_item_id=record.work_item_id, open_only=True)
            if r.id != record.id
        ]
        if others:
            con.execute("BEGIN")
            try:
                store(con, items.get(record.work_item_id))
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        else:
            items.move_with(
                record.work_item_id, "ready", f"answered {record.id}", actor="human", write=store
            )
        return _get(con, record.id)


def store_summary(
    trace: Trace,
    escalation_id: str,
    summary: Mapping[str, Any],
    *,
    run_id: str,
    agent_ref: str,
    now: Callable[[], datetime] | None = None,
) -> Summary:
    """Store the summary of one record and return it. A record has at most one summary.

    `summary` is the agent's output as a JSON object; `run_id` and `agent_ref` name the run that
    wrote it, so the summary can be traced to its cost. The caller checks the summary first
    (`runtime.escalation_summary.ungrounded`); this function stores what it is given. Raises
    `EscalationNotFound` for an unknown id and `InvalidEscalation`, storing nothing, when the
    record already has a summary or `run_id` or `agent_ref` is empty.
    """
    if not run_id.strip() or not agent_ref.strip():
        raise InvalidEscalation("a summary needs the run and the agent that wrote it")
    stamp = _stamp(now)
    with trace.connection() as con:
        _get(con, escalation_id)
        if _summary_row(con, escalation_id) is not None:
            raise InvalidEscalation(
                f"{escalation_id} already has a summary",
                hint=f"factory escalations show {escalation_id} shows it",
            )
        con.execute(
            "INSERT INTO escalation_summaries VALUES (?, ?, ?, ?, ?)",
            [escalation_id, run_id, agent_ref, json.dumps(dict(summary)), stamp],
        )
        row = _summary_row(con, escalation_id)
    assert row is not None
    return row


def get_summary(trace: Trace, escalation_id: str) -> Summary | None:
    """Return the summary of one record, or None when it has none.

    Raises `EscalationNotFound` for an unknown or malformed id.
    """
    with trace.connection() as con:
        _get(con, escalation_id)
        return _summary_row(con, escalation_id)


def _summary_row(con: duckdb.DuckDBPyConnection, escalation_id: str) -> Summary | None:
    row = con.execute(
        "SELECT escalation_id, run_id, agent_ref, summary, timezone('UTC', created_at)"
        " FROM escalation_summaries WHERE escalation_id = ?",
        [escalation_id],
    ).fetchone()
    if row is None:
        return None
    esc_id, run_id, agent_ref, summary, created_at = row
    return Summary(esc_id, run_id, agent_ref, json.loads(summary), created_at.replace(tzinfo=UTC))


def with_answers(ticket: Ticket, records: Iterable[Escalation]) -> Ticket:
    """Return `ticket` with the answered records' questions and answers appended to its body.

    The answers go under `ANSWERS_HEADING`, oldest first, so an agent reads them as part of the
    ticket and no agent's input schema changes. Open records are skipped. With no answered record
    the ticket comes back unchanged.
    """
    answered = sorted((r for r in records if not r.is_open), key=lambda r: _number(r.id))
    if not answered:
        return ticket
    parts = [ANSWERS_HEADING, ""]
    for record in answered:
        parts += [
            f"### {record.id} ({record.trigger})",
            "",
            "Question:",
            "",
            _quote(record.question),
            "",
            "Answer:",
            "",
            _quote(record.answer or ""),
            "",
        ]
    section = "\n".join(parts).rstrip()
    body = f"{ticket.body.rstrip()}\n\n{section}" if ticket.body.strip() else section
    return ticket.model_copy(update={"body": body})


def ticket_for_run(trace: Trace, item: WorkItem) -> Ticket:
    """Return the ticket a pipeline's next run should see: `item.ticket()` with its answers."""
    return with_answers(item.ticket(), list_escalations(trace, work_item_id=item.id))


def render_markdown(record: Escalation, summary: Summary | None = None) -> str:
    """Return the record as Markdown, the diff, agent outputs and suite tail in `<details>`.

    With a `summary`, a `## Summary` section comes right after the title: the paragraph, the
    options numbered with what each takes and the evidence it quotes, and the run that wrote it.
    This is what `factory escalations show` prints, and what an issue may quote.
    """
    runs = ", ".join(f"`{run}`" for run in record.pipeline_run_ids) or "none"
    raised = f"{_iso(record.created_at)} by `{record.actor}`"
    if record.pipeline_run_id:
        raised += f" in run `{record.pipeline_run_id}`"
    lines = [
        f"# {record.id}: {record.work_item_id} escalated ({record.trigger})",
        "",
        *(_summary_lines(summary) if summary is not None else []),
        "## Question",
        "",
        record.question,
        "",
        "| | |",
        "| --- | --- |",
        f"| Work item | {record.work_item_id}, was `{record.from_status}` |",
        f"| Raised | {raised} |",
        f"| Cost at the stop | ${record.cost_usd:.4f} |",
        f"| Pipeline runs | {runs} |",
        f"| Status | {'open' if record.is_open else 'answered'} |",
        "",
        "## Answer",
        "",
    ]
    if record.is_open:
        lines.append(f"Not answered yet. `factory escalations answer {record.id} --text TEXT`")
    else:
        lines += [
            record.answer or "",
            "",
            f"From `{record.answer_source}` at {_iso(record.answered_at) if record.answered_at else ''}.",
        ]
    lines.append("")
    if record.diff:
        lines += _details("Workspace diff", record.diff, "diff")
    for output in record.agent_outputs:
        title = f"Output of {output.agent}" + (f" (run `{output.run_id}`)" if output.run_id else "")
        lines += _details(title, output.output, "json" if _is_json(output.output) else "")
    if record.suite_tail:
        lines += _details("Suite output, last lines", record.suite_tail, "")
    return "\n".join(lines).rstrip() + "\n"


def _summary_lines(summary: Summary) -> list[str]:
    lines = ["## Summary", "", str(summary.summary.get("paragraph", "")).strip(), ""]
    for number, option in enumerate(summary.summary.get("options", []), start=1):
        head = f"{number}. **{option.get('label', '')}** ({option.get('takes', '')})"
        lines += [
            f"{head}: {option.get('cost', '')}",
            "",
            f"   Evidence: {_quote_inline(str(option.get('evidence', '')))}",
            "",
        ]
    lines += [f"Written by `{summary.agent_ref}` in run `{summary.run_id}`.", ""]
    return lines


def _quote_inline(text: str) -> str:
    fence = "`" * (_longest_backtick_run(text) + 1)
    body = " ".join(text.split())
    pad = " " if body.startswith("`") or body.endswith("`") else ""
    return f"{fence}{pad}{body}{pad}{fence}"


def cut_head(text: str, limit: int) -> str:
    """Return `text` with at most `limit` characters kept from its start, the cut marked."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[cut: {len(text) - limit} more characters after the first {limit}]"


def cut_tail(text: str, limit: int) -> str:
    """Return `text` with at most `limit` characters kept from its end, the cut marked."""
    if len(text) <= limit:
        return text
    return f"[cut: {len(text) - limit} earlier characters before the last {limit}]\n{text[-limit:]}"


def _details(summary: str, text: str, language: str) -> list[str]:
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    return [
        "<details>",
        f"<summary>{summary}</summary>",
        "",
        f"{fence}{language}",
        text.rstrip("\n"),
        fence,
        "",
        "</details>",
        "",
    ]


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _get(con: duckdb.DuckDBPyConnection, escalation_id: str) -> Escalation:
    if not _ID.fullmatch(escalation_id):
        raise EscalationNotFound(
            f"no escalation {escalation_id}", hint="ids look like esc-1; factory escalations list"
        )
    row = con.execute(
        f"SELECT {_COLUMNS} FROM escalations WHERE id = ?", [escalation_id]
    ).fetchone()
    if row is None:
        raise EscalationNotFound(
            f"no escalation {escalation_id}", hint="factory escalations list shows every id"
        )
    return _record(row)


def _record(row: tuple[Any, ...]) -> Escalation:
    (
        esc_id,
        item_id,
        created_at,
        actor,
        run_id,
        trigger,
        question,
        from_status,
        run_ids,
        cost,
        diff,
        outputs,
        suite_tail,
        answer_text,
        source,
        answered_at,
    ) = row
    return Escalation(
        id=esc_id,
        work_item_id=item_id,
        created_at=created_at.replace(tzinfo=UTC),
        actor=actor,
        pipeline_run_id=run_id,
        trigger=trigger,
        question=question,
        from_status=from_status,
        pipeline_run_ids=tuple(json.loads(run_ids)),
        cost_usd=float(cost),
        diff=diff,
        agent_outputs=tuple(
            AgentOutput(o["agent"], o["run_id"], o["output"]) for o in json.loads(outputs)
        ),
        suite_tail=suite_tail,
        answer=answer_text,
        answer_source=source,
        answered_at=answered_at.replace(tzinfo=UTC) if answered_at else None,
    )


def _max_number(con: duckdb.DuckDBPyConnection) -> int:
    ids = con.execute("SELECT id FROM escalations").fetchall()
    return max((_number(i) for (i,) in ids), default=0)


def _number(esc_id: str) -> int:
    match = _ID.fullmatch(esc_id)
    return int(match.group(1)) if match else 0


def _stamp(now: Callable[[], datetime] | None) -> datetime:
    at = (now or (lambda: datetime.now(UTC)))()
    if at.tzinfo is None:
        raise ValueError("now() must return a timezone-aware datetime")
    return at.astimezone(UTC)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")
