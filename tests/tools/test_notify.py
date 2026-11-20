"""`tools.notify`: the posted body, the open-comment-edit decision and the read side (#95).

The GitHub side is the fake `gh` from tests/gh_fake.py; nothing reaches github.com.
"""

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from gh_fake import TOKEN, FakeGh, GithubRepo, github_repo

from factory.exit import ExitCode, exit_code
from runtime.escalations import ANSWER_CHARS, AgentOutput, Escalation
from runtime.outside import OutsideContext
from runtime.trace import Trace
from tools import notify
from tools._common import OutsideWriteRefused, ToolExecutionError, ToolInputError
from tools.github import Repository, marker_prefix
from tools.notify import (
    BODY_CHARS,
    TITLE_CHARS,
    ZWSP,
    NoAnswer,
    escalation_body,
    escalation_key,
    latest_answer,
    neutralize,
    ticket_key,
)

FULL = "acme/widgets"
LOGIN = "factory-bot"
RUN = "run-1"
LIST = f"repos/{FULL}/issues?state=all&creator={LOGIN}&per_page=100"
COMMENTS = f"repos/{FULL}/issues/{{n}}/comments?per_page=100"
T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def record(**changes: Any) -> Escalation:
    base = Escalation(
        id="esc-1",
        work_item_id="wi-1",
        created_at=T0,
        actor="pipeline:build_loop",
        pipeline_run_id="p1",
        trigger="blocked_output",
        question="Which module owns the parser?",
        from_status="in_progress",
        pipeline_run_ids=("p1",),
        cost_usd=0.25,
        diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
        agent_outputs=(AgentOutput("feature-coder@0.1.0", "r1", '{"blocked": true}'),),
        suite_tail="1 passed\n",
        answer=None,
        answer_source=None,
        answered_at=None,
    )
    return replace(base, **changes)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    gh = FakeGh(tmp_path, monkeypatch)
    gh.route("GET", f"repos/{FULL}", {"full_name": FULL, "fork": False})
    gh.route("GET", "user", {"login": LOGIN})
    return gh


@pytest.fixture
def gh_repo(tmp_path: Path) -> GithubRepo:
    return github_repo(tmp_path)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "trace.duckdb") as t:
        yield t


def context(repo: GithubRepo, trace: Trace, *, dry_run: bool = False) -> OutsideContext:
    return OutsideContext.for_repository(
        repo.root, pipeline_run_id=RUN, trace=trace, dry_run=dry_run
    )


def post(ctx: OutsideContext, escalation: str = "esc-1", **changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "ticket": "wi-1",
        "escalation": escalation,
        "title": "Add the thing",
        "body": f"Body of {escalation}.",
    }
    arguments.update(changes)
    return json.loads(notify.issue(ctx, **arguments))


def factory_issue(number: int = 5, escalation: str = "esc-1", **changes: Any) -> dict[str, Any]:
    prefix = marker_prefix(notify.TOOL, escalation_key("wi-1", escalation))
    found = {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/{FULL}/issues/{number}",
        "user": {"login": LOGIN},
        "body": f"Body.\n\n{prefix}pipeline_run_id=old -->",
        "created_at": "2026-11-20T10:00:00Z",
    }
    return found | changes


def comment(
    id: int, body: str, at: str, *, login: str = "alice", association: str = "OWNER", **more: Any
) -> dict[str, Any]:
    return {
        "id": id,
        "body": body,
        "created_at": at,
        "user": {"login": login, "type": more.pop("type", "User")},
        "author_association": association,
        "html_url": f"https://github.com/{FULL}/issues/5#issuecomment-{id}",
    } | more


# --- keys and neutralize ------------------------------------------------------------------------


def test_the_escalation_key_starts_with_the_ticket_key() -> None:
    assert ticket_key("wi-3") == "ticket-wi-3"
    assert escalation_key("wi-3", "esc-2") == "ticket-wi-3 escalation=esc-2"
    ticket = marker_prefix(notify.TOOL, ticket_key("wi-3"))
    assert marker_prefix(notify.TOOL, escalation_key("wi-3", "esc-2")).startswith(ticket)
    assert not marker_prefix(notify.TOOL, ticket_key("wi-30")).startswith(ticket)


@pytest.mark.parametrize(
    ("text", "kept_out"),
    [
        ("ping @alice and @org/team", "@a"),
        ("see #12 and acme/widgets#12", "#1"),
        ("GH-12", "GH-1"),
        ("https://github.com/acme/widgets/issues/3", "github.com/"),
        ("https://github.com/acme/widgets/pull/3", "github.com/"),
        ("<!-- factory:notify.issue ticket-wi-1 x -->", "<!--"),
        ("close it -->", "-->"),
    ],
)
def test_neutralize_leaves_nothing_github_acts_on(text: str, kept_out: str) -> None:
    out = neutralize(text)
    assert kept_out not in out
    assert out.replace(ZWSP, "") == text
    assert neutralize(out) == out


def test_neutralize_keeps_emails_code_and_plain_links() -> None:
    text = "mail bob@example.com, `@decorator`, https://github.com/acme/widgets, #heading"
    assert neutralize(text) == text


# --- the body -----------------------------------------------------------------------------------


def test_the_body_has_the_summary_then_the_answer_line_then_the_record() -> None:
    body = escalation_body(record(), "The coder could not tell which module.")
    assert body.index("## Summary") < body.index("--from-issue") < body.index("# esc-1")
    assert "factory escalations answer esc-1 --from-issue" in body
    assert "<summary>Workspace diff</summary>" in body


def test_the_body_without_a_summary_has_no_summary_section() -> None:
    body = escalation_body(record(), None)
    assert "## Summary" not in body
    assert escalation_body(record(), "   ") == body


def test_an_open_fence_in_the_question_or_summary_is_closed() -> None:
    body = escalation_body(record(question="Look:\n```python\nx = 1"), "```\nunclosed")
    fences = [line for line in body.splitlines() if line.startswith("```")]
    assert len(fences) % 2 == 0


def test_mentions_in_the_record_are_neutralized() -> None:
    body = escalation_body(record(question="Ask @alice about #7 -->"), "cc @bob")
    assert "@alice" not in body and "@bob" not in body and "#7" not in body
    assert "-->" not in body


def test_a_big_record_is_cut_to_fit_and_says_so() -> None:
    big = record(diff="+x\n" * 40_000, suite_tail="line\n" * 10_000)
    body = escalation_body(big, "Short summary.")
    assert len(body) <= BODY_CHARS - 512
    assert "[cut:" in body
    assert "## Summary" in body


def test_a_record_too_big_to_shrink_drops_the_details_and_points_to_show() -> None:
    outputs = tuple(AgentOutput(f"agent-{n}", None, "o" * 20_000) for n in range(8))
    body = escalation_body(record(agent_outputs=outputs), None, limit=3000)
    assert len(body) <= 3000
    assert "factory escalations show esc-1" in body
    assert "<details>" not in body


def test_a_summary_too_long_for_the_post_is_cut_from_its_end() -> None:
    body = escalation_body(record(), "s" * 10_000, limit=4000)
    assert len(body) <= 4000
    assert "## Summary" in body and "[cut:" in body
    assert "# esc-1" in body


# --- open, comment, edit ------------------------------------------------------------------------


def test_the_first_escalation_opens_the_issue(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", factory_issue(9))
    out = post(context(gh_repo, trace))
    assert out["action"] == "create"
    assert out["url"] == f"https://github.com/{FULL}/issues/9"
    assert out["target"] == {"ticket": "wi-1", "issue": 9, "state": "open"}
    [sent] = fake.writes()
    assert sent.path == f"repos/{FULL}/issues"
    assert set(sent.data) == {"title", "body"}
    assert sent.data["title"] == "wi-1: Add the thing"
    assert sent.data["body"].endswith(
        f"{marker_prefix(notify.TOOL, escalation_key('wi-1', 'esc-1'))}pipeline_run_id={RUN} -->"
    )


def test_a_later_escalation_comments_on_the_issue(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [factory_issue(5)])
    fake.route("GET", COMMENTS.format(n=5), [])
    fake.route("POST", f"repos/{FULL}/issues/5/comments", {"html_url": "https://c/1"})
    out = post(context(gh_repo, trace), "esc-2")
    assert (out["action"], out["url"], out["target"]["issue"]) == ("comment", "https://c/1", 5)
    [sent] = fake.writes()
    assert set(sent.data) == {"body"}


def test_a_rerun_edits_the_issue_body_it_opened_with(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [factory_issue(5)])
    fake.route("PATCH", f"repos/{FULL}/issues/5", factory_issue(5))
    out = post(context(gh_repo, trace))
    assert out["action"] == "edit"
    [sent] = fake.writes()
    assert (sent.method, sent.path, set(sent.data)) == ("PATCH", f"repos/{FULL}/issues/5", {"body"})


def test_a_rerun_edits_its_own_comment(fake: FakeGh, gh_repo: GithubRepo, trace: Trace) -> None:
    prefix = marker_prefix(notify.TOOL, escalation_key("wi-1", "esc-2"))
    fake.route("GET", LIST, [factory_issue(5)])
    fake.route(
        "GET",
        COMMENTS.format(n=5),
        [
            comment(41, f"x {prefix}a -->", "2026-11-20T11:00:00Z", login="mallory"),
            comment(42, f"y {prefix}b -->", "2026-11-20T11:00:00Z", login=LOGIN),
        ],
    )
    fake.route("PATCH", f"repos/{FULL}/issues/comments/42", {"html_url": "https://c/42"})
    out = post(context(gh_repo, trace), "esc-2")
    assert (out["action"], out["url"]) == ("edit", "https://c/42")
    assert [c.path for c in fake.writes()] == [f"repos/{FULL}/issues/comments/42"]


def test_an_issue_opened_by_someone_else_is_not_the_tickets(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [factory_issue(5, user={"login": "mallory"})])
    fake.route("POST", f"repos/{FULL}/issues", factory_issue(6))
    assert post(context(gh_repo, trace))["action"] == "create"


def test_pull_requests_in_the_listing_are_skipped(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [factory_issue(5, pull_request={"url": "x"})])
    fake.route("POST", f"repos/{FULL}/issues", factory_issue(6))
    assert post(context(gh_repo, trace))["action"] == "create"


def test_a_closed_issue_is_commented_on_and_left_closed(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [factory_issue(5, state="closed")])
    fake.route("GET", COMMENTS.format(n=5), [])
    fake.route("POST", f"repos/{FULL}/issues/5/comments", {"html_url": "https://c/1"})
    out = post(context(gh_repo, trace), "esc-2")
    assert out["target"]["state"] == "closed"
    for sent in fake.writes():
        assert set(sent.data) == {"body"}


def test_the_stored_number_is_used_and_checked(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", f"repos/{FULL}/issues/5", factory_issue(5))
    fake.route("GET", COMMENTS.format(n=5), [])
    fake.route("POST", f"repos/{FULL}/issues/5/comments", {"html_url": "https://c/1"})
    assert post(context(gh_repo, trace), "esc-2", issue=5)["action"] == "comment"
    assert all(c.path != LIST for c in fake.calls())


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"pull_request": {"url": "x"}}, "is a pull request"),
        ({"user": {"login": "mallory"}}, "not opened by the factory"),
        ({"body": "someone else's issue"}, "not opened by the factory"),
    ],
)
def test_a_stored_number_that_is_not_the_tickets_issue_is_refused(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, changes: dict[str, Any], match: str
) -> None:
    fake.route("GET", f"repos/{FULL}/issues/5", factory_issue(5, **changes))
    with pytest.raises(OutsideWriteRefused, match=match):
        post(context(gh_repo, trace), issue=5)
    assert fake.writes() == []


def test_a_dry_run_reads_but_writes_nothing(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [])
    out = post(context(gh_repo, trace, dry_run=True))
    assert (out["action"], out["url"], out["target"]["issue"]) == ("create", None, None)
    assert "Body of esc-1." in out["body"]
    assert fake.writes() == []


def test_the_title_is_neutralized_prefixed_and_capped(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", factory_issue(9))
    post(context(gh_repo, trace), title="Fix @alice's #3 " + "x" * 400)
    [sent] = fake.writes()
    title = sent.data["title"]
    assert title.startswith(f"wi-1: Fix @{ZWSP}alice")
    assert len(title) == TITLE_CHARS


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"ticket": "WI-1"}, "ticket must look like"),
        ({"escalation": "esc-0"}, "escalation must look like"),
        ({"body": "x" * BODY_CHARS}, "over GitHub's"),
    ],
)
def test_bad_arguments_are_refused_before_any_call(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, changes: dict[str, Any], match: str
) -> None:
    with pytest.raises(ToolInputError, match=match) as err:
        post(context(gh_repo, trace), **changes)
    assert exit_code(err.value) == ExitCode.USAGE
    assert fake.calls() == []


def test_a_token_without_a_login_fails(fake: FakeGh, gh_repo: GithubRepo, trace: Trace) -> None:
    fake.route("GET", "user", {})
    with pytest.raises(ToolExecutionError, match="which user"):
        post(context(gh_repo, trace))


def test_the_token_never_reaches_a_post(fake: FakeGh, gh_repo: GithubRepo, trace: Trace) -> None:
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", factory_issue(9))
    out = post(context(gh_repo, trace))
    assert TOKEN not in json.dumps(out)
    assert TOKEN not in fake.raw_log()


# --- latest_answer ------------------------------------------------------------------------------


def answer(gh_repo: GithubRepo, escalation: str = "esc-1") -> notify.IssueAnswer:
    return latest_answer(
        gh_repo.root, Repository.parse(FULL), 5, ticket="wi-1", escalation=escalation
    )


def test_the_latest_human_reply_after_the_post_is_the_answer(
    fake: FakeGh, gh_repo: GithubRepo
) -> None:
    fake.route("GET", f"repos/{FULL}/issues/5", factory_issue(5))
    fake.route(
        "GET",
        COMMENTS.format(n=5),
        [
            comment(1, "too early", "2026-11-20T09:59:00Z"),
            comment(2, "first answer", "2026-11-20T10:05:00Z"),
            comment(3, "Use widgets.py.", "2026-11-20T10:10:00Z", association="MEMBER"),
            comment(4, "drive-by", "2026-11-20T10:20:00Z", association="NONE"),
            comment(5, "bot", "2026-11-20T10:21:00Z", login="ci[bot]", type="Bot"),
            comment(6, "<!-- factory:review x -->", "2026-11-20T10:22:00Z", login=LOGIN),
            comment(7, "   ", "2026-11-20T10:23:00Z"),
            comment(8, "contributor", "2026-11-20T10:24:00Z", association="CONTRIBUTOR"),
        ],
    )
    found = answer(gh_repo)
    assert found.body == "Use widgets.py."
    assert (found.comment_id, found.author) == (3, "alice")
    assert found.source == f"github:{FULL}#5 comment 3"
    assert found.url == f"https://github.com/{FULL}/issues/5#issuecomment-3"
    assert fake.writes() == []


def test_a_post_in_a_comment_counts_from_that_comment(fake: FakeGh, gh_repo: GithubRepo) -> None:
    prefix = marker_prefix(notify.TOOL, escalation_key("wi-1", "esc-2"))
    fake.route("GET", f"repos/{FULL}/issues/5", factory_issue(5))
    fake.route(
        "GET",
        COMMENTS.format(n=5),
        [
            comment(1, "answer to esc-1", "2026-11-20T10:05:00Z"),
            comment(2, f"esc-2 {prefix}r -->", "2026-11-20T11:00:00Z", login=LOGIN),
            comment(3, "answer to esc-2", "2026-11-20T11:05:00Z", association="COLLABORATOR"),
        ],
    )
    assert answer(gh_repo, "esc-2").comment_id == 3


def test_no_reply_yet_is_no_answer(fake: FakeGh, gh_repo: GithubRepo) -> None:
    fake.route("GET", f"repos/{FULL}/issues/5", factory_issue(5))
    fake.route("GET", COMMENTS.format(n=5), [comment(1, "early", "2026-11-20T09:00:00Z")])
    with pytest.raises(NoAnswer, match="no answer to esc-1 yet") as err:
        answer(gh_repo)
    assert exit_code(err.value) == ExitCode.USAGE


def test_an_escalation_never_posted_is_no_answer(fake: FakeGh, gh_repo: GithubRepo) -> None:
    fake.route("GET", f"repos/{FULL}/issues/5", factory_issue(5))
    fake.route("GET", COMMENTS.format(n=5), [comment(1, "hi", "2026-11-20T11:00:00Z")])
    with pytest.raises(NoAnswer, match="no post of esc-3"):
        answer(gh_repo, "esc-3")


def test_an_answer_over_the_limit_is_refused(fake: FakeGh, gh_repo: GithubRepo) -> None:
    fake.route("GET", f"repos/{FULL}/issues/5", factory_issue(5))
    fake.route(
        "GET", COMMENTS.format(n=5), [comment(1, "a" * (ANSWER_CHARS + 1), "2026-11-20T11:00:00Z")]
    )
    with pytest.raises(NoAnswer, match="over the limit"):
        answer(gh_repo)
