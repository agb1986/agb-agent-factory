"""Post an escalation to its ticket's GitHub issue, and take a person's answer from that issue (#95).

`notify_escalation` is what a pipeline calls after it escalates: it renders the record with
`tools.notify.escalation_body`, posts it through the outside write `notify.issue`
(`runtime.outside.write`), stores the issue's number on the work item, and returns one entry for
the pipeline's report. It never raises: a refused or failed post is the entry's `outcome`, so the
post cannot change the item's end state or the pipeline's exit code.

`answer_from_issue` is `factory escalations answer <id> --from-issue`: it reads the latest
qualifying comment with `tools.notify.latest_answer` and stores it through
`runtime.escalations.answer`, the same path as `--text`. It only reads GitHub.

The rules are in docs/CONVENTIONS.md (Escalations, Outside writes).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.exit import UsageError
from registry.resolver import AgentSpec
from runtime import escalations
from runtime.escalations import Escalation, InvalidEscalation
from runtime.outside import TAG_PREFIX, OutsideContext, write
from runtime.trace import Trace
from runtime.work_items import WorkItem, WorkItems
from tools._common import OutsideWriteRefused
from tools.github import Repository, origin_repository
from tools.notify import TOOL, IssueAnswer, escalation_body, latest_answer

__all__ = ["IssueAnswered", "answer_from_issue", "issue_repository", "notify_escalation"]


@dataclass(frozen=True)
class IssueAnswered:
    """What `answer_from_issue` did: the answered record and the comment it took the answer from."""

    record: Escalation
    comment: IssueAnswer


def notify_escalation(
    trace: Trace,
    escalation_id: str,
    *,
    agent: AgentSpec,
    context: OutsideContext,
    summary: str | None = None,
) -> dict[str, Any]:
    """Post escalation `escalation_id` to its ticket's issue for `agent`, and return a report entry.

    `agent` is the resolved manifest the write is made for (`escalation-summary`, which lists
    `notify.issue` in `tools.allow`); `context` is the pipeline's outside-write context; `summary`
    is the stored summary, or None when there is none. The post goes to the issue the work item
    stores, or the one the ticket's marker finds, or a new one; on a real run the issue's number
    is then stored on the item with `WorkItems.set_issue_number`.

    The entry is `{"outcome", "action", "url", "issue", "error"}`, plus `body` in a dry run (the
    planned post). `outcome` is `written`, `dry_run`, `refused` (the guard, the tool or a check
    here said no, before anything was sent) or `failed` (anything else, which may have reached
    GitHub). Never raises for a post that could not be made.
    """
    entry: dict[str, Any] = {
        "outcome": "failed",
        "action": None,
        "url": None,
        "issue": None,
        "error": None,
    }
    try:
        record = escalations.get(trace, escalation_id)
        items = WorkItems(trace)
        item = items.get(record.work_item_id)
        _check_repository(item, context.target)
        arguments: dict[str, Any] = {
            "ticket": item.id,
            "escalation": record.id,
            "title": item.title,
            "body": escalation_body(record, summary),
        }
        if item.issue_number is not None:
            arguments["issue"] = item.issue_number
        result = write(TOOL, arguments, agent=agent, context=context)
    except (OutsideWriteRefused, UsageError) as exc:
        return {**entry, "outcome": "refused", "error": _message(exc)}
    except Exception as exc:  # noqa: BLE001 - a failed post is reported, it does not stop the run
        return {**entry, "error": _message(exc)}
    entry |= {"action": result["action"], "url": result["url"]}
    number = _posted_issue(context)
    entry["issue"] = number
    if context.dry_run:
        return {**entry, "outcome": "dry_run", "body": result["body"]}
    entry["outcome"] = "written"
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        try:
            items.set_issue_number(item.id, number)
        except Exception as exc:  # noqa: BLE001 - the post was made; report the miss
            entry["error"] = f"posted, but the issue number was not stored: {_message(exc)}"
    return entry


def answer_from_issue(
    trace: Trace, escalation_id: str, *, cwd: str | Path | None = None
) -> IssueAnswered:
    """Answer escalation `escalation_id` with the latest qualifying comment on its ticket's issue.

    The issue is the one the work item stores, on the repository `issue_repository` names; `cwd`
    is where `gh` runs when the item's repo is `owner/name` (the current directory by default).
    The comment is the one `tools.notify.latest_answer` returns; the answer's source is
    `github:<owner/name>#<N> comment <id>`. Only reads GitHub.

    Raises `InvalidEscalation` (exit 2) when the record is already answered or the item has no
    issue; `tools.notify.NoAnswer` (exit 2) when no comment answers it; and whatever
    `escalations.answer` raises, exactly as for `--text`. Nothing is stored when it raises.
    """
    record = escalations.get(trace, escalation_id)
    if not record.is_open:
        raise InvalidEscalation(
            f"{record.id} is already answered",
            hint=f"factory escalations show {record.id} shows the answer",
        )
    item = WorkItems(trace).get(record.work_item_id)
    if item.issue_number is None:
        raise InvalidEscalation(
            f"{item.id} has no GitHub issue to read an answer from",
            hint=f"answer with factory escalations answer {record.id} --text TEXT",
        )
    repository, root = issue_repository(item, cwd=cwd)
    comment = latest_answer(
        root, repository, item.issue_number, ticket=item.id, escalation=record.id
    )
    answered = escalations.answer(trace, record.id, comment.body, comment.source)
    return IssueAnswered(record=answered, comment=comment)


def issue_repository(item: WorkItem, *, cwd: str | Path | None = None) -> tuple[Repository, Path]:
    """Return the github.com repository of `item`'s issue, and the directory to run `gh` in.

    An `owner/name` repo is that repository, `gh` running in `cwd` (the current directory by
    default); an absolute path is the repository its `origin` remote names, `gh` running there.
    Raises `InvalidEscalation` (exit 2) when the item names no repository, and `ToolInputError`
    (exit 2) when the path has no github.com origin.
    """
    if not item.repo:
        raise InvalidEscalation(
            f"{item.id} names no repository, so it has no issue",
            hint="answer with --text instead",
        )
    if item.repo.startswith("/"):
        return origin_repository(item.repo), Path(item.repo)
    return Repository.parse(item.repo), Path(cwd) if cwd is not None else Path.cwd()


def _check_repository(item: WorkItem, target: Repository) -> None:
    """Refuse a post when the item's `owner/name` repo is not the pipeline's target."""
    if item.repo and not item.repo.startswith("/") and not target.same(item.repo):
        raise OutsideWriteRefused(
            f"{TOOL} refused: {item.id} is on {item.repo}, the target is {target.full_name}"
        )


def _posted_issue(context: OutsideContext) -> Any:
    """Return the issue number the latest `notify.issue` row of this pipeline run recorded."""
    number = None
    for row in context.trace.pipeline_runs(context.pipeline_run_id):
        decision = row.decision_json if isinstance(row.decision_json, Mapping) else {}
        target = decision.get("target")
        if row.tag == f"{TAG_PREFIX}{TOOL}" and isinstance(target, Mapping):
            number = target.get("issue")
    return number


def _message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) else str(exc) or type(exc).__name__
