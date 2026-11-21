"""Tests for `factory review --pr N --comment`: pipelines/review_comment.py and its CLI wiring.

GitHub is a fake `gh` (tests/gh_fake.py), the models are the fakes of test_review_panel.py, and
the trace store is under `tmp_path`: nothing reaches GitHub or an API.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from gh_fake import FakeGh, git, github_repo

from factory.cli import main
from factory.exit import ExitCode
from pipelines import review_panel
from pipelines.review_comment import (
    PullRequest,
    comment_text,
    failure_section,
    finding_comments,
    resolve_pull_request,
    review_body,
)
from pipelines.review_panel import Panel, resolve_panel, run_panel
from runtime.trace import Trace
from tests.pipelines.test_judge_gate_wiring import CLEAR_SHIP
from tests.pipelines.test_review_panel import (
    SHIPPING,
    FakeJev,
    FakeRunner,
    _finding,
    _judgement,
)
from tests.pipelines.test_triage_wiring import SKIPPABLE
from tools._common import OutsideWriteRefused, ToolInputError, ToolNotFoundError
from tools.github import marker

FULL = "acme/widgets"
PATCH = "@@ -1 +1,3 @@\n print('hello')\n+print('a')\n+print('b')"
BOT = {"login": "factory-bot"}
REVIEWS = f"repos/{FULL}/pulls/7/reviews?per_page=100"
NOTES = f"repos/{FULL}/issues/7/comments?per_page=100"


@dataclass(frozen=True)
class Pull:
    """The local repository of a pull request: `base` and `head` are its commits."""

    root: Path
    base: str
    head: str


@pytest.fixture
def pull(tmp_path: Path) -> Pull:
    repo = github_repo(tmp_path)
    git(repo.root, "checkout", "-q", "-b", "feature")
    (repo.root / "app.py").write_text("print('hello')\nprint('a')\nprint('b')\n")
    git(repo.root, "commit", "-q", "-am", "second")
    return Pull(repo.root, repo.head, git(repo.root, "rev-parse", "HEAD"))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pull: Pull) -> FakeGh:
    gh = FakeGh(tmp_path, monkeypatch)
    sides = {
        "base": {"ref": "main", "sha": pull.base, "repo": {"full_name": FULL}},
        "head": {"ref": "feature", "sha": pull.head, "repo": {"full_name": FULL}},
    }
    gh.route("GET", f"repos/{FULL}", {"full_name": FULL, "fork": False, "default_branch": "main"})
    gh.route(
        "GET", f"repos/{FULL}/pulls/7", {"number": 7, "title": "Greet", "state": "open", **sides}
    )
    files = [{"filename": "app.py", "status": "modified", "patch": PATCH}]
    gh.route("GET", f"repos/{FULL}/pulls/7/files?per_page=100", files)
    gh.route("GET", "user", BOT)
    gh.route("GET", REVIEWS, [])
    gh.route("GET", NOTES, [])
    gh.route("POST", f"repos/{FULL}/pulls/7/reviews", {"html_url": "https://r/1"})
    gh.route("POST", f"repos/{FULL}/issues/7/comments", {"html_url": "https://c/1"})
    return gh


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return path


@pytest.fixture(scope="module")
def panel() -> Panel:
    return resolve_panel(allow_unpublished=True)


def _judgement_on_app(*findings: dict[str, Any]) -> dict[str, Any]:
    merged = [f | {"sources": ["code-reviewer@1.2.0"]} for f in findings]
    return _judgement(SHIPPING) | {"merged_findings": merged}


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: FakeRunner | None = None,
    jev: FakeJev | None = None,
) -> FakeRunner:
    runner = runner or FakeRunner(_judgement_on_app(_finding("app.py", 2, 2, "major")))
    monkeypatch.setattr(review_panel, "run", runner)
    monkeypatch.setattr(review_panel, "run_jev", jev or FakeJev())
    return runner


def _review(pull: Pull, *extra: str) -> int:
    return main(["review", str(pull.root), "--pr", "7", "--allow-unpublished", "--json", *extra])


def _document(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def _rows(db: Path, document: dict[str, Any], tag: str) -> list[Any]:
    with Trace(db) as trace:
        return [r for r in trace.pipeline_runs(document["pipeline_run_id"]) if r.tag == tag]


# --- resolving the pull request


def test_the_pull_request_resolves_to_its_merge_base_and_head(pull: Pull, fake: FakeGh) -> None:
    resolved = resolve_pull_request(pull.root, 7)
    assert resolved == PullRequest(7, pull.base, pull.head)


def test_a_head_commit_that_is_not_local_says_what_to_fetch(pull: Pull, fake: FakeGh) -> None:
    fake.route(
        "GET",
        f"repos/{FULL}/pulls/7",
        {
            "number": 7,
            "state": "open",
            "base": {"ref": "main", "sha": pull.base, "repo": {"full_name": FULL}},
            "head": {"ref": "feature", "sha": "f" * 40, "repo": {"full_name": FULL}},
        },
    )
    with pytest.raises(ToolNotFoundError, match="head commit") as raised:
        resolve_pull_request(pull.root, 7)
    assert raised.value.hint == "fetch it first: git fetch origin pull/7/head"


def test_a_pull_request_without_a_commit_is_not_found(pull: Pull, fake: FakeGh) -> None:
    fake.route("GET", f"repos/{FULL}/pulls/7", {"number": 7, "base": {}, "head": {}})
    with pytest.raises(ToolNotFoundError, match="no base commit"):
        resolve_pull_request(pull.root, 7)


# --- the comment and the body, from a result


@pytest.fixture
def judged(panel: Panel, pull: Pull, tmp_path: Path) -> Iterator[Any]:
    runner = FakeRunner(
        _judgement_on_app(
            _finding("app.py", 2, 2, "minor"),
            _finding("app.py", 2, 3, "blocker") | {"title": "Worse", "suggested_fix": "Fix it."},
            _finding("other.py", 9, 9, "info"),
        )
    )
    with Trace(tmp_path / "unit.duckdb") as trace:
        yield run_panel(
            panel, pull.root, pull.base, pull.head, trace, runner=runner, jev_runner=FakeJev()
        )


def test_findings_on_one_line_share_a_comment_with_the_worst_first(judged: Any) -> None:
    comments = finding_comments(judged)
    assert [(c["path"], c["line"]) for c in comments] == [("app.py", 2), ("other.py", 9)]
    first = comments[0]["body"]
    assert first.index("**blocker** (logic): Worse") < first.index("**minor** (logic)")
    assert "\n\n---\n\n" in first
    assert "Suggested fix: Fix it." in first
    assert set(comments[0]) == {"path", "line", "body"}


def test_the_body_gives_the_verdict_score_rationale_and_commit(judged: Any, pull: Pull) -> None:
    body = review_body(judged, PullRequest(7, pull.base, pull.head))
    assert body.startswith("## Review: ship (weighted score 0.")
    assert "The comparison is flipped." in body
    assert "- correctness_risk: 0.10. Checked." in body
    assert "The judge merged 3 findings." in body
    assert body.endswith(f"Reviewed commit `{pull.head[:12]}`.")


def test_a_skipped_judge_gives_a_body_without_a_score_and_no_comments(
    panel: Panel, pull: Pull, tmp_path: Path
) -> None:
    with Trace(tmp_path / "unit.duckdb") as trace:
        result = run_panel(
            panel,
            pull.root,
            pull.base,
            pull.head,
            trace,
            runner=FakeRunner(),
            jev_runner=FakeJev(gate_answers=CLEAR_SHIP),
        )
    assert finding_comments(result) == []
    body = review_body(result, PullRequest(7, pull.base, pull.head))
    assert "the judge did not run" in body
    assert "weighted score" not in body
    assert "- code-reviewer@1.2.0: " in body


def test_a_review_triage_skipped_says_no_reviewer_looked_and_how_to_force_one(
    panel: Panel, pull: Pull, tmp_path: Path
) -> None:
    assert panel.triage is not None
    with Trace(tmp_path / "unit.duckdb") as trace:
        result = run_panel(
            panel,
            pull.root,
            pull.base,
            pull.head,
            trace,
            runner=FakeRunner(),
            jev_runner=FakeJev(SKIPPABLE),
            act_on_triage=True,
            sample=lambda: False,
        )
    assert result.skipped is True
    assert finding_comments(result) == []
    body = review_body(result, PullRequest(7, pull.base, pull.head))
    assert body.startswith("## Review: skipped by triage\n\n")
    assert "(low_risk: needs_review p=0.020)" in body
    assert "no reviewer and no judge looked at it" in body
    assert "`factory review <repo_path> --pr 7 --comment --no-triage`" in body
    assert "found nothing." in body
    assert "weighted score" not in body
    assert body.endswith(f"Reviewed commit `{pull.head[:12]}`.")


def test_comment_text_and_failure_section() -> None:
    created = {"outcome": "written", "action": "create", "url": "https://r/1"}
    assert comment_text(created) == "comment: created the review at https://r/1"
    assert comment_text(created | {"action": "edit"}) == "comment: edited the review at https://r/1"
    planned = {"outcome": "dry_run", "action": "create", "url": None}
    assert comment_text(planned) == "comment: dry run, would create the review"
    refused = failure_section(OutsideWriteRefused("no"))
    assert refused["outcome"] == "refused"
    assert failure_section(ToolInputError("bad", hint="h")) == {
        "outcome": "refused",
        "error": "bad",
        "hint": "h",
    }
    assert failure_section(ToolNotFoundError("gone"))["outcome"] == "failed"
    assert comment_text(failure_section(ToolNotFoundError("gone"))) == "comment: failed, gone"


# --- factory review --pr N --comment


def test_without_comment_the_document_has_no_comment_and_nothing_is_written(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    runner = _run_cli(monkeypatch)
    assert _review(pull) == ExitCode.OK
    document = _document(capsys)
    assert document["comment"] is None
    assert document["base"] == pull.base and document["head"] == pull.head
    # The fake triage routes to code and security, so two reviewers and the judge run.
    assert len(runner.calls) == 3
    assert fake.writes() == []


def test_comment_posts_a_review_with_inline_findings_at_the_head_commit(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    _run_cli(monkeypatch)
    assert _review(pull, "--comment") == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    comment = document["comment"]
    assert comment["outcome"] == "written" and comment["action"] == "create"
    assert comment["url"] == "https://r/1"
    assert "## Review: ship" in comment["body"]
    assert "comment: created the review at https://r/1" in captured.err
    (post,) = fake.writes()
    assert post.path == f"repos/{FULL}/pulls/7/reviews"
    assert post.data["commit_id"] == pull.head
    assert [(c["path"], c["line"]) for c in post.data["comments"]] == [("app.py", 2)]
    assert "**major** (logic): Problem" in post.data["comments"][0]["body"]
    (row,) = _rows(db, document, "outside:github.comment_pr")
    assert row.decision_json["outcome"] == "written"
    assert row.parent_run_id == "row-judge"
    assert row.pipeline_run_id == document["pipeline_run_id"]


def test_a_rerun_edits_the_factorys_earlier_review(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    mark = marker("github.comment_pr", "pr-7", "earlier-run")
    fake.route("GET", REVIEWS, [{"id": 6, "user": BOT, "body": f"old\n{mark}"}])
    fake.route(
        "GET",
        f"repos/{FULL}/pulls/7/reviews/6/comments?per_page=100",
        [{"id": 61, "path": "app.py", "line": 2, "body": "old"}],
    )
    fake.route("PUT", f"repos/{FULL}/pulls/7/reviews/6", {"html_url": "https://r/6"})
    fake.route("PATCH", f"repos/{FULL}/pulls/comments/61", {})
    _run_cli(monkeypatch)
    assert _review(pull, "--comment") == ExitCode.OK
    comment = _document(capsys)["comment"]
    assert comment["action"] == "edit" and comment["url"] == "https://r/6"
    assert [w.method for w in fake.writes()] == ["PUT", "PATCH"]


def test_dry_run_reads_and_sends_nothing_but_records_the_plan(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    _run_cli(monkeypatch)
    assert _review(pull, "--comment", "--dry-run") == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    comment = document["comment"]
    assert comment["outcome"] == "dry_run" and comment["url"] is None
    assert comment["action"] == "create"
    assert "comment: dry run, would create the review" in captured.err
    assert fake.writes() == []
    (row,) = _rows(db, document, "outside:github.comment_pr")
    assert row.decision_json["outcome"] == "dry_run"


def test_a_skipped_judge_posts_a_summary_comment_with_no_inline_findings(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    _run_cli(monkeypatch, jev=FakeJev(gate_answers=CLEAR_SHIP))
    assert _review(pull, "--comment") == ExitCode.OK
    document = _document(capsys)
    comment = document["comment"]
    assert comment["url"] == "https://c/1"
    assert "the judge did not run" in comment["body"]
    (post,) = fake.writes()
    assert post.path == f"repos/{FULL}/issues/7/comments"
    (row,) = _rows(db, document, "outside:github.comment_pr")
    assert row.parent_run_id is None


def test_a_finding_outside_the_diff_is_listed_in_the_body(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    runner = FakeRunner(
        _judgement_on_app(_finding("app.py", 2, 2, "major"), _finding("app.py", 40, 40, "minor"))
    )
    _run_cli(monkeypatch, runner=runner)
    assert _review(pull, "--comment") == ExitCode.OK
    comment = _document(capsys)["comment"]
    assert "On lines outside the diff:" in comment["body"]
    assert "`app.py:40`" in comment["body"]
    (post,) = fake.writes()
    assert [c["line"] for c in post.data["comments"]] == [2]


def test_a_moved_head_is_refused_and_the_review_is_still_printed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    _run_cli(monkeypatch)
    moved = {
        "number": 7,
        "state": "open",
        "base": {"ref": "main", "sha": pull.base, "repo": {"full_name": FULL}},
        "head": {"ref": "feature", "sha": "e" * 40, "repo": {"full_name": FULL}},
    }
    real_post = review_panel.post_review

    def moving(result: Any, resolved: PullRequest, context: Any) -> dict[str, Any]:
        fake.route("GET", f"repos/{FULL}/pulls/7", moved)
        return real_post(result, resolved, context)

    monkeypatch.setattr(review_panel, "post_review", moving)
    assert _review(pull, "--comment") == ExitCode.USAGE
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    comment = document["comment"]
    assert comment["outcome"] == "refused"
    assert comment["hint"] == "its head moved since the review; review it again"
    assert "hint: its head moved since the review; review it again" in captured.err
    assert fake.writes() == []
    (row,) = _rows(db, document, "outside:github.comment_pr")
    assert row.decision_json["outcome"] == "refused"


def test_a_failed_github_call_is_reported_and_the_review_is_still_printed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    fake.route("POST", f"repos/{FULL}/pulls/7/reviews", None, status=500)
    _run_cli(monkeypatch)
    code = _review(pull, "--comment")
    assert code == ExitCode.ERROR
    document = _document(capsys)
    assert document["ship"] is True
    assert document["comment"]["outcome"] == "failed"
    (row,) = _rows(db, document, "outside:github.comment_pr")
    assert row.decision_json["outcome"] == "failed"


def test_a_missing_token_exits_4_before_any_model_is_called(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    runner = _run_cli(monkeypatch)
    monkeypatch.delenv("GH_TOKEN")
    assert _review(pull, "--comment") == ExitCode.AUTH_OR_CONFIG
    captured = capsys.readouterr()
    assert captured.out == ""
    assert runner.calls == []


def test_a_missing_commit_exits_3_before_any_model_is_called(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
) -> None:
    fake.route(
        "GET",
        f"repos/{FULL}/pulls/7",
        {
            "number": 7,
            "state": "open",
            "base": {"ref": "main", "sha": pull.base, "repo": {"full_name": FULL}},
            "head": {"ref": "feature", "sha": "d" * 40, "repo": {"full_name": FULL}},
        },
    )
    runner = _run_cli(monkeypatch)
    assert _review(pull, "--comment") == ExitCode.NOT_FOUND
    assert "git fetch origin pull/7/head" in capsys.readouterr().err
    assert runner.calls == []


@pytest.mark.parametrize(
    "args, message",
    [
        (["--pr", "7", "--base", "main"], "--pr replaces --base and --head"),
        ([], "give --pr N, or both --base and --head"),
        (["--comment"], "give --pr N, or both --base and --head"),
        (["--base", "main", "--head", "feature", "--comment"], "--comment and --dry-run need --pr"),
        (["--pr", "7", "--dry-run"], "--dry-run needs --comment"),
    ],
)
def test_bad_flag_combinations_are_usage_errors_that_cost_nothing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    pull: Pull,
    fake: FakeGh,
    db: Path,
    args: list[str],
    message: str,
) -> None:
    runner = _run_cli(monkeypatch)
    assert main(["review", str(pull.root), "--allow-unpublished", *args]) == ExitCode.USAGE
    assert message in capsys.readouterr().err
    assert runner.calls == []
    assert fake.calls() == []
