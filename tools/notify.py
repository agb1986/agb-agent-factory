"""`notify.issue`: post an escalation to the ticket's GitHub issue, and read a person's answer back.

The post is an outside write under ADR 0003 (`docs/decisions/0003-outside-writes.md`). Pipeline code
calls it through `runtime.outside.write("notify.issue", ...)`, from `runtime.notify`; no model is
given it. The rules are in docs/CONVENTIONS.md (Outside writes, Escalations).

One issue per ticket. The issue is found by its marker, `<!-- factory:notify.issue ticket-<id> `,
in a body written by the token's GitHub user, or by the number the work item stores. Each post
carries the per-escalation marker `<!-- factory:notify.issue ticket-<id> escalation=<esc> ...`,
which starts with the ticket's, so:

- the first escalation of a ticket opens the issue, the post being its body;
- a later escalation of the same ticket comments on that issue;
- a rerun for the same escalation edits the post it made, the issue body or the comment.

A closed issue is commented on or edited as an open one would be and is never reopened; the
result's target says `state: closed`. Nothing here closes, reopens, labels, assigns or locks an
issue: the only fields ever sent are `title` and `body` when opening, and `body` otherwise.

`escalation_body` renders what is posted. `latest_answer` is the read side for
`factory escalations answer <id> --from-issue`: it reads the issue's comments and returns the
latest one a person wrote after the escalation was posted.
"""

import re
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from runtime.escalations import ANSWER_CHARS, Escalation, cut_head, cut_tail, render_markdown
from tools._common import (
    DEFAULT_MAX_BYTES,
    OutsideWriteRefused,
    ToolExecutionError,
    ToolInputError,
    ToolSpec,
)
from tools.github import (
    Repository,
    _check_target,
    _login,
    _mine,
    _result,
    _string,
    footer,
    gh,
    marker_prefix,
)

if TYPE_CHECKING:
    from runtime.outside import OutsideContext

__all__ = [
    "ANSWER_ASSOCIATIONS",
    "BODY_CHARS",
    "TITLE_CHARS",
    "TOOL",
    "IssueAnswer",
    "NoAnswer",
    "escalation_body",
    "escalation_key",
    "issue",
    "latest_answer",
    "neutralize",
    "ticket_key",
]

TOOL = "notify.issue"

BODY_CHARS = 65536
"""GitHub's limit on an issue or comment body, in characters; the post, footer included, fits it."""

TITLE_CHARS = 256
"""GitHub's limit on an issue title, in characters."""

ANSWER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
"""The `author_association` values whose comments count as an answer."""

ZWSP = "\u200b"
"""The zero-width space `neutralize` puts inside a mention, a reference or a comment delimiter."""

_TICKET = re.compile(r"wi-[1-9][0-9]*")
_ESCALATION = re.compile(r"esc-[1-9][0-9]*")
_FACTORY_MARKER = "<!-- factory:"
_MENTION = re.compile(r"(?<![A-Za-z0-9_`])@(?=[A-Za-z0-9])")
_REFERENCE = re.compile(r"#(?=[0-9])")
_GH_REFERENCE = re.compile(r"\b(GH)-(?=[0-9])", re.IGNORECASE)
_ISSUE_URL = re.compile(
    r"github\.com(?=/[^/\s]+/[^/\s]+/(?:issues|pull|pulls|discussions)/[0-9])", re.IGNORECASE
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SHRINK = (1.0, 0.5, 0.25, 0.1)
_DROPPED = (
    "The workspace diff, the agent outputs and the suite output did not fit in one GitHub post"
    " and are left out; `factory escalations show {id}` prints them."
)


class NoAnswer(ToolInputError):
    """The issue has no comment that can answer the escalation. Exit 2."""


@dataclass(frozen=True)
class IssueAnswer:
    """The comment `latest_answer` found: its text, id, author, link, and the source to record."""

    body: str
    comment_id: int
    author: str | None
    url: str | None
    source: str


def ticket_key(ticket: str) -> str:
    """Return the marker key of a ticket's issue: `ticket-<id>`."""
    return f"ticket-{ticket}"


def escalation_key(ticket: str, escalation: str) -> str:
    """Return the marker key of one escalation's post: `ticket-<id> escalation=<esc>`.

    It starts with `ticket_key(ticket)` and a space, so the ticket's marker prefix finds it too.
    """
    return f"{ticket_key(ticket)} escalation={escalation}"


def neutralize(text: str) -> str:
    """Return `text` with nothing GitHub would act on: no mention, reference or HTML comment.

    A zero-width space goes after an `@` that starts a mention, after a `#` before a digit (so
    `#12` and `owner/repo#12` link nothing), between `GH` and `-12`, inside `github.com` in an
    issue, pull request or discussion URL (so the post leaves no cross-reference on it), and
    inside `<!--` and `-->` (so posted text can neither open an HTML comment nor close a marker,
    and a marker search never matches text the record carried). Applying it twice changes nothing
    more. The text is otherwise kept: a diff copied from a post may carry the zero-width spaces,
    and `factory escalations show` prints the record as stored.
    """
    text = text.replace("<!--", f"<!{ZWSP}--").replace("-->", f"--{ZWSP}>")
    text = _MENTION.sub(f"@{ZWSP}", text)
    text = _REFERENCE.sub(f"#{ZWSP}", text)
    text = _GH_REFERENCE.sub(lambda m: f"{m.group(1)}{ZWSP}-", text)
    return _ISSUE_URL.sub(lambda m: f"{m.group(0)[:6]}{ZWSP}{m.group(0)[6:]}", text)


def _close_fences(text: str) -> str:
    """Return `text` with a closing fence added when it leaves a code fence open.

    A free-text field (the question, the summary) that opens a fence and never closes it would
    swallow everything after it in the post, the footer and marker included.
    """
    open_fence: str | None = None
    for line in text.splitlines():
        found = _FENCE.match(line)
        if found is None:
            continue
        run = found.group(1)
        if open_fence is None:
            open_fence = run
        elif (
            run[0] == open_fence[0] and len(run) >= len(open_fence) and not line.strip()[len(run) :]
        ):
            open_fence = None
    return text if open_fence is None else f"{text.rstrip()}\n{open_fence}\n"


def _compose(record: Escalation, summary: str | None, note: str | None = None) -> str:
    parts = []
    if summary is not None and summary.strip():
        parts += ["## Summary", "", _close_fences(summary.strip()).rstrip(), ""]
    parts += [
        (
            f"To answer, reply on this issue, then run `factory escalations answer {record.id}"
            " --from-issue`. Only the latest comment after this post by an owner, member or"
            " collaborator counts, and only once."
        ),
        "",
    ]
    if note is not None:
        parts += [note, ""]
    parts.append(render_markdown(record).rstrip())
    return neutralize("\n".join(parts)) + "\n"


def escalation_body(record: Escalation, summary: str | None = None, *, limit: int = 0) -> str:
    """Return the Markdown `notify.issue` posts for `record`, the footer not included.

    The summary, when given, comes first under `## Summary`, then a line saying how to answer,
    then `render_markdown(record)`. The question and the summary have any open code fence closed;
    the diff, outputs and suite tail are already fenced longer than any backtick run they hold.
    The whole text is passed through `neutralize`.

    The result fits `limit` characters, `BODY_CHARS` less room for the footer when 0. To fit, the
    diff and the agent outputs are cut from their end and the suite tail from its start, to half,
    a quarter and a tenth of their size in turn; when that is not enough, all three are left out
    and a line says `factory escalations show` prints them. The question is already capped at
    4000 characters; a summary that still does not fit is cut from its end.
    """
    limit = limit or BODY_CHARS - _FOOTER_ROOM
    record = replace(record, question=_close_fences(record.question))
    for factor in _SHRINK:
        cut = replace(
            record,
            diff=cut_head(record.diff, int(len(record.diff) * factor)),
            agent_outputs=tuple(
                replace(o, output=cut_head(o.output, int(len(o.output) * factor)))
                for o in record.agent_outputs
            ),
            suite_tail=cut_tail(record.suite_tail, int(len(record.suite_tail) * factor)),
        )
        text = _compose(cut, summary)
        if len(text) <= limit:
            return text
    bare = replace(record, diff="", agent_outputs=(), suite_tail="")
    note = _DROPPED.format(id=record.id)
    text = _compose(bare, summary, note)
    if len(text) > limit and summary:
        room = max(0, len(summary) - (len(text) - limit) - 200)
        text = _compose(bare, cut_head(summary, room), note)
    return text if len(text) <= limit else _close_fences(cut_head(text, max(0, limit - 200)))


_FOOTER_ROOM = 512
"""Characters `escalation_body` leaves for the footer: run id, attribution and marker."""


def _title(title: str, ticket: str) -> str:
    text = neutralize(" ".join(_string(title, "title").split()))
    if not text.startswith(f"{ticket}:"):
        text = f"{ticket}: {text}"
    return text if len(text) <= TITLE_CHARS else f"{text[: TITLE_CHARS - 1]}…"


def _id(value: Any, pattern: re.Pattern[str], what: str, example: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ToolInputError(f"{what} must look like {example}: {value!r}")
    return value


def issue(
    context: OutsideContext,
    ticket: str,
    escalation: str,
    title: str,
    body: str,
    issue: int | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Post escalation `escalation` of work item `ticket` to the ticket's issue, or edit the post.

    `body` is the Markdown to post, normally `escalation_body(...)`; it is neutralized again and
    the footer added. `title` names the issue when one is opened, prefixed with the ticket id.
    `issue`, when given, is the number the work item stores; otherwise the issue is found by the
    ticket's marker among the issues the token's user opened. The decision follows the module
    docstring. In a dry run every read runs and the result shows what the real run would do.

    Returns JSON `{"action": "create" | "comment" | "edit", "url", "body", "target"}`, the target
    `{"ticket", "issue", "state"}` (`issue` None when a dry run would open one). Raises
    `ToolInputError` for a bad id, an empty title or body, or a body over `BODY_CHARS` with the
    footer; `OutsideWriteRefused` when the target check fails, or `issue` is a pull request or an
    issue the factory did not open for this ticket; and the `gh` errors otherwise.
    """
    ticket = _id(ticket, _TICKET, "ticket", "wi-1")
    escalation = _id(escalation, _ESCALATION, "escalation", "esc-1")
    heading = _title(title, ticket)
    text = neutralize(_string(body, "body").rstrip())
    key = escalation_key(ticket, escalation)
    text += f"\n\n{footer(TOOL, key, context.pipeline_run_id)}"
    if len(text) > BODY_CHARS:
        raise ToolInputError(
            f"the post is {len(text)} characters, over GitHub's {BODY_CHARS}",
            hint="build the body with tools.notify.escalation_body, which fits it",
        )
    root = context.repo_path
    _check_target(context, TOOL)
    full = context.target.full_name
    login = _login(gh("user", root))
    if login is None:
        raise ToolExecutionError("GitHub did not say which user GH_TOKEN is")
    ticket_prefix = marker_prefix(TOOL, ticket_key(ticket))
    post_prefix = marker_prefix(TOOL, key)
    found = _ticket_issue(context, issue, ticket, ticket_prefix, login)
    if found is None:
        target = {"ticket": ticket, "issue": None, "state": None}
        if context.dry_run:
            return _out("create", None, text, target, max_bytes)
        made = gh(
            f"repos/{full}/issues", root, method="POST", data={"title": heading, "body": text}
        )
        target = {**target, "issue": made.get("number"), "state": made.get("state")}
        return _out("create", made.get("html_url"), text, target, max_bytes)
    number = found["number"]
    target = {"ticket": ticket, "issue": number, "state": found.get("state")}
    if post_prefix in (found.get("body") or ""):
        url = None
        if not context.dry_run:
            edited = gh(f"repos/{full}/issues/{number}", root, method="PATCH", data={"body": text})
            url = edited.get("html_url")
        return _out("edit", url, text, target, max_bytes)
    comments = gh(f"repos/{full}/issues/{number}/comments?per_page=100", root, paginate=True)
    earlier = _mine(comments, post_prefix, login)
    if earlier is not None:
        url = None
        if not context.dry_run:
            edited = gh(
                f"repos/{full}/issues/comments/{earlier['id']}",
                root,
                method="PATCH",
                data={"body": text},
            )
            url = edited.get("html_url")
        return _out("edit", url, text, target, max_bytes)
    if context.dry_run:
        return _out("comment", None, text, target, max_bytes)
    posted = gh(f"repos/{full}/issues/{number}/comments", root, method="POST", data={"body": text})
    return _out("comment", posted.get("html_url"), text, target, max_bytes)


def _ticket_issue(
    context: OutsideContext, number: int | None, ticket: str, prefix: str, login: str
) -> dict[str, Any] | None:
    """Return the ticket's issue: `number` checked, else the one the marker finds, else None."""
    full = context.target.full_name
    root = context.repo_path
    if number is not None:
        found = gh(f"repos/{full}/issues/{number}", root)
        if "pull_request" in found:
            raise OutsideWriteRefused(f"{TOOL} refused: #{number} is a pull request, not an issue")
        if prefix not in (found.get("body") or "") or _login(found.get("user")) != login:
            raise OutsideWriteRefused(
                f"{TOOL} refused: issue {number} was not opened by the factory for {ticket}"
            )
        return found
    query = urllib.parse.urlencode({"state": "all", "creator": login, "per_page": 100})
    listed = gh(f"repos/{full}/issues?{query}", root, paginate=True)
    issues = [item for item in listed if "pull_request" not in item]
    return _mine(sorted(issues, key=lambda item: item.get("number") or 0), prefix, login)


def _out(action: str, url: str | None, body: str, target: dict[str, Any], max_bytes: int) -> str:
    return _result({"action": action, "url": url, "body": body, "target": target}, max_bytes)


def latest_answer(
    repo_path: str | Path,
    repository: Repository,
    number: int,
    *,
    ticket: str,
    escalation: str,
) -> IssueAnswer:
    """Return the comment on issue `number` that answers `escalation`. Only reads.

    The post is the issue body or the comment carrying the escalation's marker. The answer is
    the latest comment created after that post that carries no factory marker (a factory post,
    whoever's token wrote it, is never an answer), whose author's `author_association` is in
    `ANSWER_ASSOCIATIONS`, is not a bot, and is not empty. This trusts everyone the repository
    lets comment with that association. The source is `github:<owner/name>#<N> comment <id>`.

    Raises `NoAnswer` (exit 2) when the escalation was never posted on the issue, when no
    comment qualifies, or when the latest one is over `ANSWER_CHARS`; and the `gh` errors.
    """
    root = Path(repo_path)
    full = repository.full_name
    found = gh(f"repos/{full}/issues/{number}", root)
    prefix = marker_prefix(TOOL, escalation_key(ticket, escalation))
    comments = gh(f"repos/{full}/issues/{number}/comments?per_page=100", root, paginate=True)
    if prefix in (found.get("body") or ""):
        posted_at = _at(found.get("created_at"))
    else:
        posts = [c for c in comments if prefix in (c.get("body") or "")]
        if not posts:
            raise NoAnswer(
                f"issue {number} has no post of {escalation}",
                hint=f"post it first, or answer with factory escalations answer {escalation} --text",
            )
        posted_at = _at(posts[0].get("created_at"))
    replies = [
        c
        for c in comments
        if _FACTORY_MARKER not in (c.get("body") or "")
        and (c.get("body") or "").strip()
        and c.get("author_association") in ANSWER_ASSOCIATIONS
        and (c.get("user") or {}).get("type") != "Bot"
        and posted_at is not None
        and (_at(c.get("created_at")) or posted_at) > posted_at
    ]
    if not replies:
        raise NoAnswer(
            f"issue {number} has no answer to {escalation} yet",
            hint="an owner, member or collaborator replies on the issue after the post",
        )
    reply = max(replies, key=lambda c: (_at(c.get("created_at")), c.get("id") or 0))
    text = reply["body"].strip()
    if len(text) > ANSWER_CHARS:
        raise NoAnswer(
            f"the latest answer on issue {number} is {len(text)} characters,"
            f" over the limit of {ANSWER_CHARS}",
            hint="shorten the comment on GitHub, or answer with --text",
        )
    return IssueAnswer(
        body=text,
        comment_id=reply.get("id"),
        author=_login(reply.get("user")),
        url=reply.get("html_url"),
        source=f"github:{full}#{number} comment {reply.get('id')}",
    )


def _at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_ID_SCHEMA = {"type": "string"}

TOOLS = (
    ToolSpec(
        name=TOOL,
        description="Post one escalation of a work item to the ticket's GitHub issue on this repository: the first escalation opens the issue, a later one comments on it, and a rerun edits its earlier post. An outside write: pipeline code calls it through runtime.outside.write; no model is given it.",
        input_schema={
            "type": "object",
            "properties": {
                "ticket": {**_ID_SCHEMA, "description": "The work item id, such as wi-1."},
                "escalation": {**_ID_SCHEMA, "description": "The escalation id, such as esc-1."},
                "title": {"type": "string", "description": "The issue title, used only when one is opened."},
                "body": {"type": "string", "description": "The post, in Markdown: tools.notify.escalation_body."},
                "issue": {"type": "integer", "minimum": 1, "description": "The issue the work item stores, when it has one."},
            },
            "required": ["ticket", "escalation", "title", "body"],
            "additionalProperties": False,
        },
        impl=issue,
        reaches_out=True,
        outside_write=True,
    ),
)  # fmt: skip
"""The tools this module declares; `tools.registry` collects them."""
