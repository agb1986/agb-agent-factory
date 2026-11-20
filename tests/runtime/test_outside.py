"""`runtime.outside`: the guard every outside write passes and the trace row it leaves.

The GitHub side is the fake `gh` from tests/gh_fake.py; nothing reaches github.com.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from gh_fake import FakeGh, GithubRepo, git, github_repo

from factory.exit import ExitCode, exit_code
from registry.resolver import AgentSpec, load_spec
from registry.spec import LLMSpec
from runtime.hooks import ToolCall
from runtime.outside import (
    TAG_PREFIX,
    OutsideContext,
    OutsideGuard,
    OutsideWriteRefused,
    planned_target,
    write,
)
from runtime.trace import Trace
from tools._common import ToolExecutionError, ToolInputError
from tools.github import Repository

FULL = "acme/widgets"
RUN = "run-1"
REPO = {"full_name": FULL, "fork": False, "default_branch": "main"}
JUDGE = Path(__file__).resolve().parents[2] / "agents" / "judge" / "agent.yaml"
CREATE = {"title": "Fix", "body": "Body."}


@pytest.fixture
def judge() -> AgentSpec:
    return load_spec(JUDGE)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    gh = FakeGh(tmp_path, monkeypatch)
    gh.route("GET", f"repos/{FULL}", REPO)
    return gh


@pytest.fixture
def gh_repo(tmp_path: Path) -> GithubRepo:
    return github_repo(tmp_path)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "trace.duckdb") as t:
        yield t


def context(
    repo: GithubRepo, trace: Trace, *, run: str = RUN, dry_run: bool = True
) -> OutsideContext:
    return OutsideContext.for_repository(
        repo.root, pipeline_run_id=run, trace=trace, dry_run=dry_run, parent_run_id="judge-run"
    )


def rows(trace: Trace) -> list[dict[str, Any]]:
    trace.ensure_schema()
    cursor = trace._connect().execute(
        "SELECT agent_ref, kind, pipeline_run_id, parent_run_id, cost_usd, decision_json, "
        "input_json, output_json, error, tag FROM traces ORDER BY started_at"
    )
    names = [column[0] for column in cursor.description]
    found = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
    for row in found:
        for key in ("decision_json", "input_json", "output_json"):
            row[key] = None if row[key] is None else json.loads(row[key])
    return found


def with_tools(agent: AgentSpec, **update: Any) -> AgentSpec:
    assert isinstance(agent, LLMSpec)
    return agent.model_copy(update={"tools": agent.tools.model_copy(update=update)})


# --- the context --------------------------------------------------------------------------------


def test_for_repository_reads_the_target_from_origin(gh_repo: GithubRepo, trace: Trace) -> None:
    ctx = context(gh_repo, trace)
    assert ctx.target == Repository("acme", "widgets")
    assert ctx.repo_path == gh_repo.root.resolve()
    assert (ctx.pipeline_run_id, ctx.dry_run, ctx.parent_run_id) == (RUN, True, "judge-run")


def test_for_repository_refuses_a_repository_without_a_github_origin(
    tmp_path: Path, trace: Trace
) -> None:
    repo = github_repo(tmp_path, origin=None)
    with pytest.raises(ToolInputError, match="has no origin remote") as err:
        context(repo, trace)
    assert exit_code(err.value) == ExitCode.USAGE


def test_planned_target_names_the_branch_only_for_create_pr(
    gh_repo: GithubRepo, trace: Trace
) -> None:
    ctx = context(gh_repo, trace)
    assert planned_target("github.create_pr", ctx, CREATE) == {
        "repository": FULL,
        "pr": None,
        "issue": None,
        "branch": f"factory/{RUN}",
    }
    assert planned_target("github.comment_pr", ctx, {"pr": 7})["branch"] is None


# --- the guard ----------------------------------------------------------------------------------


def decide(ctx: OutsideContext, agent: AgentSpec, name: str, by: str | None = None) -> str | None:
    call = ToolCall(name=name, arguments={"pr": 7}, agent=by or agent.address)
    verdict = OutsideGuard(ctx, agent).before_tool(call)
    return None if verdict.allowed else verdict.message


def test_the_guard_allows_an_outside_write_the_manifest_lists(
    gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    ctx = context(gh_repo, trace)
    assert decide(ctx, judge, "github.comment_pr") is None
    assert decide(ctx, judge, "github.create_pr") is None


def test_the_guard_refuses_a_tool_that_is_not_an_outside_write(
    gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    ctx = context(gh_repo, trace)
    for name in ("git.read_file", "github.read_pr", "fs.write_file", "no.such_tool"):
        assert decide(ctx, judge, name) == f"{name} is not an outside write"


def test_the_guard_refuses_a_call_made_for_another_agent(
    gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    message = decide(context(gh_repo, trace), judge, "github.comment_pr", by="fixer@1.0.0")
    assert message == f"the call is for fixer@1.0.0, not {judge.address}"


@pytest.mark.parametrize(
    "update",
    [
        {"allow": ("git.read_file",)},
        {"allow": (), "deny": ()},
    ],
)
def test_the_guard_refuses_an_agent_that_does_not_list_the_write(
    gh_repo: GithubRepo, trace: Trace, judge: AgentSpec, update: dict
) -> None:
    agent = with_tools(judge, **update)
    message = decide(context(gh_repo, trace), agent, "github.comment_pr")
    assert message == f"{judge.address} does not list github.comment_pr in tools.allow"


def test_the_guard_refuses_a_denied_write_even_when_allow_lists_it(
    gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    # A manifest cannot list one tool in both; a spec built in code still must not get through.
    agent = with_tools(judge, deny=("github.comment_pr",))
    assert decide(context(gh_repo, trace), agent, "github.comment_pr") == (
        f"{judge.address} does not list github.comment_pr in tools.allow"
    )


def test_the_guard_refuses_when_origin_moved_or_went_away(
    gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    ctx = context(gh_repo, trace)
    git(gh_repo.root, "remote", "set-url", "origin", "git@github.com:evil/widgets.git")
    assert decide(ctx, judge, "github.comment_pr") == (
        "origin is evil/widgets, the target is acme/widgets"
    )
    git(gh_repo.root, "remote", "remove", "origin")
    message = decide(ctx, judge, "github.comment_pr")
    assert message is not None
    assert message.startswith("the repository has no github.com origin: ")


# --- write and its trace row --------------------------------------------------------------------


def test_write_records_one_row_for_a_dry_run(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    got = write("github.create_pr", CREATE, agent=judge, context=context(gh_repo, trace))
    assert got["action"] == "create" and got["url"] is None
    assert got["body"].startswith("Body.\n\n")
    assert not fake.writes()
    (row,) = rows(trace)
    assert row["agent_ref"] == judge.address
    assert (row["kind"], row["pipeline_run_id"], row["parent_run_id"]) == ("llm", RUN, "judge-run")
    assert row["cost_usd"] == 0.0
    assert row["tag"] == f"{TAG_PREFIX}github.create_pr"
    assert row["input_json"] == CREATE
    assert row["output_json"] == got
    assert row["error"] is None
    assert row["decision_json"] == {
        "tool": "github.create_pr",
        "target": {"repository": FULL, "pr": None, "issue": None, "branch": f"factory/{RUN}"},
        "outcome": "dry_run",
        "allow": True,
        "reason": None,
        "dry_run": True,
        "url": None,
    }


def test_write_records_a_written_row_with_the_url_and_the_new_pr(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    fake.route("POST", f"repos/{FULL}/pulls", {"number": 12, "html_url": "https://pr/12"})
    ctx = context(gh_repo, trace, dry_run=False)
    got = write("github.create_pr", CREATE, agent=judge, context=ctx)
    assert got["url"] == "https://pr/12"
    (row,) = rows(trace)
    decision = row["decision_json"]
    assert (decision["outcome"], decision["url"], decision["dry_run"]) == (
        "written",
        "https://pr/12",
        False,
    )
    assert decision["target"]["pr"] == 12
    assert git(gh_repo.bare, "rev-parse", f"refs/heads/factory/{RUN}") == gh_repo.head


def test_a_refusal_is_recorded_and_raises_exit_1(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    agent = with_tools(judge, allow=("git.read_file",))
    with pytest.raises(OutsideWriteRefused, match="does not list") as err:
        write("github.create_pr", CREATE, agent=agent, context=context(gh_repo, trace))
    assert exit_code(err.value) == ExitCode.ERROR
    assert not fake.calls()
    (row,) = rows(trace)
    assert row["decision_json"]["outcome"] == "refused"
    assert row["decision_json"]["allow"] is False
    assert row["decision_json"]["reason"].endswith("does not list github.create_pr in tools.allow")
    assert row["error"].startswith("OutsideWriteRefused: github.create_pr refused: ")
    assert row["output_json"] is None


def test_arguments_that_do_not_fit_are_refused_before_anything_is_sent(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    ctx = context(gh_repo, trace)
    with pytest.raises(ToolInputError, match="missing required arguments: body"):
        write("github.create_pr", {"title": "Fix"}, agent=judge, context=ctx)
    with pytest.raises(ToolInputError, match="unknown arguments: max_bytes"):
        write("github.create_pr", {**CREATE, "max_bytes": 1}, agent=judge, context=ctx)
    assert not fake.calls()
    first, second = rows(trace)
    assert first["decision_json"]["outcome"] == second["decision_json"]["outcome"] == "refused"
    assert first["decision_json"]["allow"] is True
    assert first["error"].startswith("ToolInputError: github.create_pr: missing")
    # Refused calls do not count for the once rule.
    write("github.create_pr", CREATE, agent=judge, context=ctx)
    assert rows(trace)[-1]["decision_json"]["outcome"] == "dry_run"


def test_the_tool_s_own_refusal_does_not_count_for_the_once_rule(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    ctx = context(gh_repo, trace)
    fake.route("GET", f"repos/{FULL}", {**REPO, "fork": True})
    with pytest.raises(OutsideWriteRefused, match="is a fork"):
        write("github.create_pr", CREATE, agent=judge, context=ctx)
    assert rows(trace)[0]["decision_json"]["outcome"] == "refused"
    fake.route("GET", f"repos/{FULL}", REPO)
    write("github.create_pr", CREATE, agent=judge, context=ctx)
    assert rows(trace)[-1]["decision_json"]["outcome"] == "dry_run"


@pytest.mark.parametrize("first_outcome", ["dry_run", "written", "failed"])
def test_a_second_write_on_the_same_target_in_one_pipeline_run_is_refused(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec, first_outcome: str
) -> None:
    ctx = context(gh_repo, trace, dry_run=first_outcome == "dry_run")
    if first_outcome == "written":
        fake.route("POST", f"repos/{FULL}/pulls", {"number": 12, "html_url": "https://pr/12"})
    if first_outcome == "failed":
        fake.route("GET", f"repos/{FULL}", fail="gh: Server Error (HTTP 502)")
        with pytest.raises(ToolExecutionError):
            write("github.create_pr", CREATE, agent=judge, context=ctx)
        fake.route("GET", f"repos/{FULL}", REPO)
    else:
        write("github.create_pr", CREATE, agent=judge, context=ctx)
    assert rows(trace)[0]["decision_json"]["outcome"] == first_outcome
    calls = len(fake.calls())
    with pytest.raises(OutsideWriteRefused, match=f"already ran on this target.*{first_outcome}"):
        write("github.create_pr", CREATE, agent=judge, context=ctx)
    assert len(fake.calls()) == calls
    # Another pipeline run is not a repeat: a rerun edits through the marker instead.
    other = context(gh_repo, trace, run="run-2")
    assert write("github.create_pr", CREATE, agent=judge, context=other)["action"] == "create"


def test_a_failed_write_records_failed_and_the_error(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    fake.route("GET", f"repos/{FULL}", fail="gh: Server Error (HTTP 502)")
    with pytest.raises(ToolExecutionError):
        write("github.create_pr", CREATE, agent=judge, context=context(gh_repo, trace))
    (row,) = rows(trace)
    assert row["decision_json"]["outcome"] == "failed"
    assert row["decision_json"]["allow"] is True
    assert row["error"] == f"ToolExecutionError: gh api GET repos/{FULL} failed"
    assert row["output_json"] is None


def test_the_once_rule_compares_the_pull_request_number(
    fake: FakeGh, gh_repo: GithubRepo, trace: Trace, judge: AgentSpec
) -> None:
    pull = {
        "number": 7,
        "base": {"repo": {"full_name": FULL}},
        "head": {"sha": "a" * 40},
    }
    for number in (7, 8):
        fake.route("GET", f"repos/{FULL}/pulls/{number}", {**pull, "number": number})
        fake.route("GET", f"repos/{FULL}/issues/{number}/comments?per_page=100", [])
        fake.route("GET", f"repos/{FULL}/pulls/{number}/reviews?per_page=100", [])
    fake.route("GET", "user", {"login": "factory-bot"})
    ctx = context(gh_repo, trace)
    write("github.comment_pr", {"pr": 7, "body": "One."}, agent=judge, context=ctx)
    write("github.comment_pr", {"pr": 8, "body": "Two."}, agent=judge, context=ctx)
    with pytest.raises(OutsideWriteRefused, match="already ran"):
        write("github.comment_pr", {"pr": 7, "body": "Again."}, agent=judge, context=ctx)
    assert [r["decision_json"]["target"]["pr"] for r in rows(trace)] == [7, 8, 7]
    assert not fake.writes()
