"""`runtime.notify`: posting an escalation for a pipeline, and `--from-issue` (#95).

The GitHub side is the fake `gh` from tests/gh_fake.py; nothing reaches github.com.
"""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from gh_fake import FakeGh, GithubRepo, github_repo
from pydantic import BaseModel

from factory.exit import ExitCode, exit_code
from registry.common_schemas import Ticket
from registry.resolver import AgentSpec, load_spec
from registry.spec import LLMSpec
from runtime import escalations
from runtime.after_escalation import after_escalation
from runtime.budget import Budget
from runtime.escalations import InvalidEscalation, escalate, get_summary, summary_markdown
from runtime.notify import answer_from_issue, issue_repository, notify_escalation
from runtime.outside import OutsideContext
from runtime.trace import Trace
from runtime.work_items import WorkItems
from tools import notify
from tools.github import marker_prefix
from tools.notify import NoAnswer, escalation_key

FULL = "acme/widgets"
LOGIN = "factory-bot"
ACTOR = "pipeline:build_loop"
LIST = f"repos/{FULL}/issues?state=all&creator={LOGIN}&per_page=100"
AGENTS = Path(__file__).resolve().parents[2] / "agents"
JUDGE = AGENTS / "judge" / "agent.yaml"
SUMMARY_AGENT = AGENTS / "escalation-summary" / "agent.yaml"


def ticket(repo: str = FULL) -> Ticket:
    return Ticket(
        kind="feature",
        title="Add the thing",
        body="Context.",
        acceptance=["the thing exists"],
        repo=repo,
        ref="main",
    )


@pytest.fixture
def agent() -> LLMSpec:
    """The escalation-summary manifest, the agent the posts are made for."""
    spec = load_spec(SUMMARY_AGENT)
    assert isinstance(spec, LLMSpec)
    assert notify.TOOL in spec.tools.allow
    return spec


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


def escalated(trace: Trace, repo: str = FULL, run: str = "p1") -> tuple[str, str]:
    """Add an item on `repo`, start run `run` on it and escalate it. Return (item, escalation)."""
    items = WorkItems(trace)
    item_id = items.add(ticket(repo)).id
    items.move(item_id, "ready", "go")
    items.link_run(item_id, run)
    items.move(item_id, "in_progress", "start", actor=ACTOR, pipeline_run_id=run)
    record = escalate(
        trace,
        item_id,
        trigger="blocked_output",
        question="Which module owns the parser?",
        reason="CoderBlockedError: blocked",
        actor=ACTOR,
        pipeline_run_id=run,
    )
    return item_id, record.id


def again(trace: Trace, item_id: str, run: str) -> str:
    """Retry `item_id` in run `run` without answering, and escalate it again."""
    items = WorkItems(trace)
    items.move(item_id, "ready", "retry")
    items.link_run(item_id, run)
    items.move(item_id, "in_progress", "again", actor=ACTOR, pipeline_run_id=run)
    return escalate(
        trace,
        item_id,
        trigger="blocked_output",
        question="And the lexer?",
        reason="CoderBlockedError: blocked again",
        actor=ACTOR,
        pipeline_run_id=run,
    ).id


def context(
    repo: GithubRepo, trace: Trace, *, run: str = "p1", dry_run: bool = False
) -> OutsideContext:
    return OutsideContext.for_repository(
        repo.root, pipeline_run_id=run, trace=trace, dry_run=dry_run
    )


def issue_json(number: int, item_id: str, escalation: str = "esc-1", **more: Any) -> dict:
    prefix = marker_prefix(notify.TOOL, escalation_key(item_id, escalation))
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/{FULL}/issues/{number}",
        "user": {"login": LOGIN},
        "body": f"Body.\n\n{prefix}pipeline_run_id=p1 -->",
        "created_at": "2026-11-20T10:00:00Z",
    } | more


# --- notify_escalation --------------------------------------------------------------------------


def test_a_post_opens_the_issue_and_stores_its_number(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: AgentSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", issue_json(9, item_id))
    entry = notify_escalation(
        trace, esc, agent=agent, context=context(gh_repo, trace), summary="It is stuck."
    )
    assert entry == {
        "outcome": "written",
        "action": "create",
        "url": f"https://github.com/{FULL}/issues/9",
        "issue": 9,
        "error": None,
    }
    assert WorkItems(trace).get(item_id).issue_number == 9
    [sent] = fake.writes()
    assert "## Summary" in sent.data["body"] and "It is stuck." in sent.data["body"]
    assert sent.data["title"] == f"{item_id}: Add the thing"


def test_a_later_run_comments_on_the_stored_issue(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: AgentSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", issue_json(9, item_id))
    notify_escalation(trace, esc, agent=agent, context=context(gh_repo, trace))
    second = again(trace, item_id, "p2")
    fake.route("GET", f"repos/{FULL}/issues/9", issue_json(9, item_id))
    fake.route("GET", f"repos/{FULL}/issues/9/comments?per_page=100", [])
    fake.route("POST", f"repos/{FULL}/issues/9/comments", {"html_url": "https://c/1"})
    entry = notify_escalation(trace, second, agent=agent, context=context(gh_repo, trace, run="p2"))
    assert (entry["outcome"], entry["action"], entry["issue"]) == ("written", "comment", 9)
    assert fake.writes()[-1].path == f"repos/{FULL}/issues/9/comments"


def test_a_second_post_in_one_run_is_refused_by_the_once_rule(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: AgentSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", issue_json(9, item_id))
    ctx = context(gh_repo, trace)
    notify_escalation(trace, esc, agent=agent, context=ctx)
    entry = notify_escalation(trace, esc, agent=agent, context=ctx)
    assert entry["outcome"] == "refused"
    assert "already ran on this target" in entry["error"]
    assert len(fake.writes()) == 1


def test_a_dry_run_returns_the_planned_post_and_stores_nothing(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: AgentSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    entry = notify_escalation(
        trace, esc, agent=agent, context=context(gh_repo, trace, dry_run=True), summary="S."
    )
    assert (entry["outcome"], entry["action"], entry["url"]) == ("dry_run", "create", None)
    assert "# esc-1" in entry["body"] and "S." in entry["body"]
    assert fake.writes() == []
    assert WorkItems(trace).get(item_id).issue_number is None


def test_an_agent_without_the_tool_is_refused(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    _, esc = escalated(trace)
    entry = notify_escalation(trace, esc, agent=load_spec(JUDGE), context=context(gh_repo, trace))
    assert entry["outcome"] == "refused"
    assert "tools.allow" in entry["error"]
    assert fake.writes() == []


def test_an_item_on_another_repository_is_refused(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: AgentSpec
) -> None:
    _, esc = escalated(trace, repo="acme/other")
    entry = notify_escalation(trace, esc, agent=agent, context=context(gh_repo, trace))
    assert entry["outcome"] == "refused"
    assert "acme/other" in entry["error"]
    assert fake.calls() == []


def test_a_github_error_is_a_failed_entry_not_an_exception(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: AgentSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", {"message": "Server Error"}, status=500, fail=True)
    entry = notify_escalation(trace, esc, agent=agent, context=context(gh_repo, trace))
    assert entry["outcome"] == "failed"
    assert entry["error"]
    assert WorkItems(trace).get(item_id).issue_number is None
    assert escalations.get(trace, esc).is_open


def test_an_unknown_escalation_is_a_failed_entry(
    gh_repo: GithubRepo, trace: Trace, agent: AgentSpec, fake: FakeGh
) -> None:
    entry = notify_escalation(trace, "esc-9", agent=agent, context=context(gh_repo, trace))
    assert entry["outcome"] == "failed"
    assert "esc-9" in entry["error"]


# --- after_escalation's notify step ----------------------------------------------------------

SUMMARY: dict[str, Any] = {
    "paragraph": "The coder stopped: it does not know which module owns the parser.",
    "options": [
        {
            "label": "Name the module",
            "takes": "another_run",
            "cost": "Another coder run with the answer.",
            "evidence": "Which module owns the parser?",
        }
    ],
}


class Runner:
    """A stand-in for `runtime.runner.run` that returns `output`, or raises it."""

    def __init__(self, output: dict[str, Any] | Exception = SUMMARY) -> None:
        self.output = output
        self.calls = 0

    def __call__(
        self,
        spec: LLMSpec,
        payload: dict[str, Any],
        trace: Trace,
        *,
        pipeline_run_id: str | None,
        run_id: str,
        budget: Budget,
    ) -> BaseModel:
        self.calls += 1
        if isinstance(self.output, Exception):
            raise self.output
        return spec.output_class.model_validate(self.output)


def after(
    trace: Trace,
    esc: str,
    outside: OutsideContext,
    agent: LLMSpec | None,
    runner: Runner | None = None,
    *,
    dry_run: bool = False,
):
    return after_escalation(
        trace,
        escalations.get(trace, esc),
        budget=Budget(1.0),
        pipeline_run_id="p1",
        dry_run=dry_run,
        summary_agent=agent,
        outside=outside,
        runner=runner or Runner(),
    )


def test_the_notify_step_posts_the_record_with_its_stored_summary(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: LLMSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", issue_json(9, item_id))
    done = after(trace, esc, context(gh_repo, trace), agent)
    assert done.summary.done
    assert done.notify.done and done.notify.reason is None
    assert done.notify.post is not None
    assert (done.notify.post["outcome"], done.notify.post["issue"]) == ("written", 9)
    assert done.to_json()["notify"] == {"done": True, "post": done.notify.post, "reason": None}
    stored = get_summary(trace, esc)
    assert stored is not None
    text = summary_markdown(stored)
    assert not text.startswith("## Summary") and text.startswith(SUMMARY["paragraph"])
    [sent] = fake.writes()
    body = sent.data["body"]
    assert "## Summary" in body and text.strip() in body and "Which module owns the parser?" in body
    assert WorkItems(trace).get(item_id).issue_number == 9
    [row] = [r for r in trace.pipeline_runs("p1") if r.tag == f"outside:{notify.TOOL}"]
    assert row.parent_run_id == done.summary.run_id


def test_without_a_summary_the_post_is_the_record_alone(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: LLMSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", issue_json(9, item_id))
    done = after(trace, esc, context(gh_repo, trace), agent, Runner(RuntimeError("down")))
    assert not done.summary.done
    assert done.notify.done
    [sent] = fake.writes()
    assert "## Summary" not in sent.data["body"]
    assert f"# {esc}" in sent.data["body"]
    [row] = [r for r in trace.pipeline_runs("p1") if r.tag == f"outside:{notify.TOOL}"]
    assert row.parent_run_id is None


@pytest.mark.parametrize(
    ("dry_run", "use_agent", "reason"),
    [
        (True, True, "dry run: nothing is posted"),
        (False, False, "no published escalation-summary agent resolved"),
    ],
)
def test_the_notify_step_is_skipped(
    fake: FakeGh,
    gh_repo: GithubRepo,
    trace: Trace,
    agent: LLMSpec,
    dry_run: bool,
    use_agent: bool,
    reason: str,
) -> None:
    _, esc = escalated(trace)
    done = after(trace, esc, context(gh_repo, trace), agent if use_agent else None, dry_run=dry_run)
    assert done.notify.to_json() == {"done": False, "post": None, "reason": reason}
    assert fake.calls() == []


def test_a_failed_post_leaves_the_item_escalated(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: LLMSpec
) -> None:
    item_id, esc = escalated(trace)
    fake.route("GET", LIST, [])
    fake.route("POST", f"repos/{FULL}/issues", {"message": "Server Error"}, status=500, fail=True)
    done = after(trace, esc, context(gh_repo, trace), agent)
    assert not done.notify.done
    assert done.notify.post is not None and done.notify.post["outcome"] == "failed"
    assert done.notify.reason is not None and done.notify.reason.startswith("the post was failed: ")
    assert WorkItems(trace).get(item_id).status == "escalated"
    assert WorkItems(trace).get(item_id).issue_number is None
    assert escalations.get(trace, esc).is_open


def test_a_post_outside_the_target_is_refused_without_reading_github(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: LLMSpec
) -> None:
    item_id, esc = escalated(trace, repo="acme/other")
    done = after(trace, esc, context(gh_repo, trace), agent)
    assert not done.notify.done
    assert done.notify.reason is not None and done.notify.reason.startswith(
        "the post was refused: "
    )
    assert fake.calls() == []
    assert WorkItems(trace).get(item_id).status == "escalated"


def test_an_outside_dry_run_returns_the_planned_post(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, agent: LLMSpec
) -> None:
    _, esc = escalated(trace)
    fake.route("GET", LIST, [])
    done = after(trace, esc, context(gh_repo, trace, dry_run=True), agent)
    assert not done.notify.done
    assert done.notify.reason == "dry run: the planned post is in the entry, nothing was sent"
    assert done.notify.post is not None and "## Summary" in done.notify.post["body"]
    assert fake.writes() == []


def test_the_notify_step_never_raises(
    gh_repo: GithubRepo, trace: Trace, agent: LLMSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, esc = escalated(trace)

    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("the summary table is gone")

    monkeypatch.setattr("runtime.after_escalation.get_summary", broken)
    done = after(trace, esc, context(gh_repo, trace), agent)
    assert done.notify.to_json() == {
        "done": False,
        "post": None,
        "reason": "RuntimeError: the summary table is gone",
    }
    assert escalations.get(trace, esc).is_open


# --- answer_from_issue --------------------------------------------------------------------------


def test_the_answer_comes_from_the_latest_collaborator_reply(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace
) -> None:
    item_id, esc = escalated(trace)
    WorkItems(trace).set_issue_number(item_id, 9)
    fake.route("GET", f"repos/{FULL}/issues/9", issue_json(9, item_id))
    fake.route(
        "GET",
        f"repos/{FULL}/issues/9/comments?per_page=100",
        [
            {
                "id": 77,
                "body": "Use widgets/parser.py.",
                "created_at": "2026-11-20T10:30:00Z",
                "user": {"login": "alice", "type": "User"},
                "author_association": "OWNER",
                "html_url": f"https://github.com/{FULL}/issues/9#issuecomment-77",
            }
        ],
    )
    done = answer_from_issue(trace, esc, cwd=gh_repo.root)
    assert done.record.answer == "Use widgets/parser.py."
    assert done.record.answer_source == f"github:{FULL}#9 comment 77"
    assert done.comment.author == "alice"
    assert WorkItems(trace).get(item_id).status == "ready"
    assert fake.writes() == []
    with pytest.raises(InvalidEscalation, match="already answered"):
        answer_from_issue(trace, esc, cwd=gh_repo.root)


def test_no_stored_issue_is_refused(trace: Trace) -> None:
    _, esc = escalated(trace)
    with pytest.raises(InvalidEscalation, match="no GitHub issue") as err:
        answer_from_issue(trace, esc)
    assert exit_code(err.value) == ExitCode.USAGE


def test_no_reply_stores_nothing(fake: FakeGh, gh_repo: GithubRepo, trace: Trace) -> None:
    item_id, esc = escalated(trace)
    WorkItems(trace).set_issue_number(item_id, 9)
    fake.route("GET", f"repos/{FULL}/issues/9", issue_json(9, item_id))
    fake.route("GET", f"repos/{FULL}/issues/9/comments?per_page=100", [])
    with pytest.raises(NoAnswer):
        answer_from_issue(trace, esc, cwd=gh_repo.root)
    assert escalations.get(trace, esc).is_open


def test_issue_repository_reads_a_path_from_its_origin(trace: Trace, gh_repo: GithubRepo) -> None:
    item = WorkItems(trace).add(ticket(str(gh_repo.root)))
    repository, root = issue_repository(item)
    assert (repository.full_name, root) == (FULL, gh_repo.root)
    named = WorkItems(trace).add(ticket())
    assert issue_repository(named, cwd="/w")[1] == Path("/w")


def test_issue_repository_refuses_an_item_without_a_repo(trace: Trace) -> None:
    item = replace(WorkItems(trace).add(ticket()), repo="")  # a row from before repo was required
    with pytest.raises(InvalidEscalation, match="names no repository"):
        issue_repository(item)
