"""Tests for pipelines/build_loop.py: the build loop, its board moves and `factory build`.

No Docker, no API key and no network. The coder is a fake that writes files in the clone the way
the real feature-coder does through `fs.write_file`, each write going through the hook it is given
first; the reviewers and the judge are fakes too, the sandbox is `FakeSandbox` and `gh` is the fake
of `tests/gh_fake.py`, whose pushes land in a bare repository under `tmp_path`.
"""

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.cli import main
from factory.exit import ExitCode, UsageError
from pipelines import build_loop
from pipelines.build_loop import (
    ACTOR,
    FEATURE_CODER,
    FILE_WRITE_REFUSED_TAG,
    BuildAgents,
    BuildResult,
    check_buildable,
    coder_for,
    findings_ticket,
    resolve_build_agents,
    run_build,
)
from pipelines.review_panel import JUDGE
from pipelines.sandbox import DirtyRepositoryError
from registry.common_schemas import Ticket
from registry.spec import LLMSpec
from runtime.budget import Budget
from runtime.hooks import ToolCall
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItemNotFound, WorkItems
from tests.gh_fake import FakeGh, GithubRepo, git, github_repo
from tests.pipelines.test_fix_loop import FakeSandbox
from tests.pipelines.test_review_panel import (
    BLOCKING,
    SHIPPING,
    FakeJev,
    _finding,
    _judgement,
    _review,
)
from tools._common import ToolInputError

RUN_ID = "pipeline-run-1"
FULL = "acme/widgets"
REPO_JSON = {"full_name": FULL, "fork": False, "default_branch": "main"}
PULL_JSON = {"number": 7, "html_url": f"https://github.com/{FULL}/pull/7"}
CODE = {
    "widgets.py": "def size() -> int:\n    return 3\n",
    "tests/test_widgets.py": "from widgets import size\n\n\ndef test_size() -> None:\n"
    "    assert size() == 3\n",
}
FAILED = b"1 failed in 0.02s\n"


def _ticket(**changes: Any) -> Ticket:
    fields: dict[str, Any] = {
        "kind": "feature",
        "title": "Widgets report their size",
        "body": "Add `size()` to widgets.py.",
        "acceptance": ["size() returns 3"],
        "repo": FULL,
        "ref": "main",
    }
    return Ticket.model_validate(fields | changes)


class BuildRunner:
    """Stands in for `runtime.runner.run`: the feature-coder, the reviewers and the judge.

    The coder asks its hook about each file of `rounds[n]` (path to content; default `CODE`, then
    one more line in round 2) and writes it when allowed, then writes `unhooked` without asking,
    as a script run through `shell.run` would. With `blocked` it writes nothing and answers
    blocked. The judge answers `judgements` in turn, the last one again once they run out. Every
    call traces a row with its own `run_id` and charges `0.25` to the budget, as the real runner
    does after a run.
    """

    def __init__(
        self,
        judgements: Sequence[Mapping[str, Any]] = (),
        *,
        rounds: Sequence[Mapping[str, str]] | None = None,
        unhooked: Mapping[str, str] | None = None,
        blocked: str | None = None,
    ) -> None:
        self.judgements = [dict(j) for j in judgements] or [_judgement(SHIPPING)]
        self.rounds = (
            [CODE, {"widgets.py": CODE["widgets.py"] + "\n\nLIMIT = 10\n"}]
            if rounds is None
            else [dict(r) for r in rounds]
        )
        self.unhooked = dict(unhooked or {})
        self.blocked = blocked
        self.calls: list[dict[str, Any]] = []
        self.judged = 0

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any] | BaseModel, trace: Trace, **kwargs: Any
    ) -> BaseModel:
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        self.calls.append({"spec": spec, "payload": data, **kwargs})
        run_id = kwargs.get("run_id") or str(uuid.uuid4())
        if spec.name == FEATURE_CODER[0]:
            output = self._code(spec, Path(kwargs["repo_path"]), kwargs["hook"])
        elif spec.name == JUDGE[0]:
            output = self.judgements[min(self.judged, len(self.judgements) - 1)]
            self.judged += 1
        else:
            findings = (
                [_finding("widgets.py", 1, 2, "major")] if spec.name == "code-reviewer" else []
            )
            output = _review(*findings)
        trace.record(
            TraceRow(
                run_id=run_id,
                pipeline_run_id=kwargs["pipeline_run_id"],
                parent_run_id=kwargs.get("parent_run_id"),
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=data,
                cost_usd=0.25,
            )
        )
        kwargs["budget"].charge(0.25)
        return spec.output_class.model_validate(output)

    def _code(self, spec: LLMSpec, clone: Path, hook: Any) -> dict[str, Any]:
        number = len(self.coder_calls())
        if self.blocked is not None:
            return {
                "files_changed": [],
                "test_files": [],
                "acceptance": [],
                "commands_run": [],
                "summary": "Cannot build it.",
                "open_questions": [],
                "blocked": True,
                "blocked_reason": self.blocked,
            }
        files = self.rounds[min(number, len(self.rounds)) - 1] if self.rounds else {}
        for path, content in files.items():
            hook.before_tool(
                ToolCall("fs.write_file", {"path": path, "content": content}, spec.address)
            )
            _write(clone, path, content)
        for path, content in self.unhooked.items():
            _write(clone, path, content)
        return {
            "files_changed": [
                {"path": path, "action": "modified", "summary": "Size."}
                for path in [*files, *self.unhooked]
            ],
            "test_files": [path for path in files if path.startswith("tests/")],
            "acceptance": [
                {
                    "criterion": "size() returns 3",
                    "tests": ["tests/test_widgets.py::test_size"],
                    "untestable_reason": None,
                }
            ],
            "commands_run": [],
            "summary": f"Added size(), round {number}.",
            "open_questions": [],
            "blocked": False,
            "blocked_reason": None,
        }

    def coder_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["spec"].name == FEATURE_CODER[0]]


def _write(clone: Path, path: str, content: str) -> None:
    target = clone / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture(scope="module")
def agents() -> BuildAgents:
    return resolve_build_agents("feature", allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh(tmp_path, monkeypatch)
    fake.route("GET", f"repos/{FULL}", REPO_JSON)
    fake.route("POST", f"repos/{FULL}/pulls", PULL_JSON)
    return fake


@pytest.fixture
def repo(tmp_path: Path, gh: FakeGh) -> GithubRepo:
    return github_repo(tmp_path)


def _ready(trace: Trace, ticket: Ticket | None = None, **kwargs: Any) -> str:
    items = WorkItems(trace)
    item = items.add(ticket or _ticket(), **kwargs)
    items.ready()
    return item.id


def _work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def _build(
    agents: BuildAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    item_id: str,
    *,
    runner: BuildRunner | None = None,
    sandbox: FakeSandbox | None = None,
    **kwargs: Any,
) -> BuildResult:
    kwargs.setdefault("jev_runner", FakeJev())
    kwargs.setdefault("work_dir", _work_dir(tmp_path))
    kwargs.setdefault("repo_path", repo.root)
    return run_build(
        agents,
        item_id,
        trace,
        out_dir=tmp_path / "out",
        sandbox=sandbox or FakeSandbox(),
        runner=runner or BuildRunner(),
        pipeline_run_id=RUN_ID,
        **kwargs,
    )


def _branches(bare: Path) -> str:
    return git(bare, "for-each-ref", "--format=%(refname)")


def _branches_tags_and_head(root: Path) -> tuple[str, str]:
    """The source repository's branches and tags, and its HEAD."""
    return git(root, "for-each-ref", "refs/heads", "refs/tags"), git(root, "rev-parse", "HEAD")


def _refusals(trace: Trace) -> list[tuple[Any, ...]]:
    trace.ensure_schema()
    return (
        trace._connect()
        .execute(
            "SELECT agent_ref, pipeline_run_id, cost_usd, decision_json, error FROM traces "
            "WHERE tag = ?",
            [FILE_WRITE_REFUSED_TAG],
        )
        .fetchall()
    )


# --- resolving the agents and checking the item


def test_the_agents_resolve_to_the_guarded_feature_coder_and_the_full_panel(
    agents: BuildAgents,
) -> None:
    assert agents.coder.address == "feature-coder@0.1.0"
    assert agents.coder.sandbox_profile == "offline"
    assert agents.coder.guard is not None and agents.coder.files is not None
    assert agents.panel.triage is not None and agents.panel.gate is not None
    assert agents.panel.judge.address.startswith("judge@")


@pytest.mark.parametrize("kind", ["feature", "bug", "docs"])
def test_every_code_kind_but_refactor_goes_to_the_feature_coder(kind: str) -> None:
    assert coder_for(kind) == FEATURE_CODER


def test_a_refactor_ticket_goes_to_the_refactorer() -> None:
    # The refactor path itself is tested in tests/pipelines/test_behaviour.py.
    assert coder_for("refactor") == ("refactorer", "^0.1")


@pytest.mark.parametrize(("kind", "hint"), [("incident", "factory hotfix"), ("research", "run")])
def test_the_other_kinds_are_refused_with_where_they_go(kind: str, hint: str) -> None:
    with pytest.raises(UsageError, match=f"does not take {kind} tickets") as info:
        coder_for(kind)
    assert hint in (info.value.hint or "")


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"guard": None}, "no action guard"),
        ({"files": None}, "no files policy"),
        ({"sandbox": False}, "not sandboxed"),
    ],
)
def test_a_coder_without_its_safeguards_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, update: dict[str, Any], message: str
) -> None:
    real = build_loop.resolve

    def weakened(name: str, *args: Any, **kwargs: Any) -> Any:
        spec = real(name, *args, **kwargs)
        return spec.model_copy(update=update) if name == FEATURE_CODER[0] else spec

    monkeypatch.setattr(build_loop, "resolve", weakened)
    with pytest.raises(UsageError, match=message):
        resolve_build_agents("feature", allow_unpublished=True)


def test_an_item_that_is_not_ready_is_refused_before_anything_runs(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item = WorkItems(trace).add(_ticket())
    runner = BuildRunner()
    with pytest.raises(UsageError, match="is todo, and the build loop takes only ready items"):
        _build(agents, repo, trace, tmp_path, item.id, runner=runner)
    assert runner.calls == []
    assert WorkItems(trace).get(item.id).pipeline_run_ids == ()
    assert not (tmp_path / "out").exists()


def test_an_incident_is_refused_by_check_buildable(trace: Trace) -> None:
    item_id = _ready(trace, _ticket(kind="incident"))
    with pytest.raises(UsageError, match="does not take incident tickets"):
        check_buildable(WorkItems(trace).get(item_id))


def test_an_unknown_item_is_not_found(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    with pytest.raises(WorkItemNotFound):
        _build(agents, repo, trace, tmp_path, "wi-99")


# --- a run that ships


def test_a_shipped_build_opens_the_pull_request_and_moves_the_item_to_in_review(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    runner, sandbox = BuildRunner(), FakeSandbox()
    before = _branches_tags_and_head(repo.root)
    result = _build(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox)

    assert result.code == ExitCode.OK
    doc = result.to_json()
    assert doc["outcome"] == "shipped" and doc["stopped"] is None
    assert doc["work_item"]["status_before"] == "ready"
    assert doc["work_item"]["status_after"] == "in_review"
    assert doc["head"] == repo.head
    assert doc["coder"] == "feature-coder@0.1.0"
    ((entry),) = doc["rounds"]
    assert entry["files_changed"] == [
        {"path": "tests/test_widgets.py", "action": "created"},
        {"path": "widgets.py", "action": "created"},
    ]
    assert entry["suite"]["passed"] and entry["panel"]["ship"]

    pull = doc["pull_request"]
    assert pull["url"] == PULL_JSON["html_url"] and pull["branch"] == f"factory/{RUN_ID}"
    assert git(repo.bare, "rev-parse", f"refs/heads/factory/{RUN_ID}") == pull["commit"]
    assert git(repo.bare, "show", f"{pull['commit']}:widgets.py") == CODE["widgets.py"].strip()
    (post,) = gh.writes()
    assert post.path == f"repos/{FULL}/pulls"
    assert post.data["head"] == f"factory/{RUN_ID}" and post.data["base"] == "main"
    assert "## Acceptance\n\n- size() returns 3" in post.data["body"]
    assert f"Work item `{item_id}`.\n\nPipeline run `{RUN_ID}`." in post.data["body"]

    # The source repository gains no branch, no tag and no change, and HEAD stays; the clone is
    # gone. The push may make git record refs/remotes/origin/factory/<run id>, depending on the
    # git version, so remote-tracking refs are not compared.
    assert _branches_tags_and_head(repo.root) == before
    assert git(repo.root, "status", "--porcelain") == ""
    assert list(_work_dir(tmp_path).iterdir()) == []
    assert sandbox.checked == 1 and len(sandbox.cleaned) == 1

    report = json.loads((tmp_path / "out" / f"{RUN_ID}.json").read_text(encoding="utf-8"))
    assert report == json.loads(json.dumps(doc))
    assert report["total_cost_usd"] == trace.cost_by_pipeline_run(RUN_ID)


def test_every_row_and_move_carries_the_one_pipeline_run_id(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    runner = BuildRunner()
    _build(agents, repo, trace, tmp_path, item_id, runner=runner)

    items = WorkItems(trace)
    assert items.get(item_id).pipeline_run_ids == (RUN_ID,)
    moves = [e for e in items.events(item_id) if e.actor == ACTOR]
    assert [(e.from_status, e.to_status) for e in moves] == [
        ("ready", "in_progress"),
        ("in_progress", "in_review"),
    ]
    assert {e.pipeline_run_id for e in moves} == {RUN_ID}
    assert {call["pipeline_run_id"] for call in runner.calls} == {RUN_ID}
    rows = trace.pipeline_runs(RUN_ID)
    (outside,) = [row for row in rows if row.tag == "outside:github.create_pr"]
    (judge,) = [r for r in rows if r.agent_ref.startswith("judge@") and r.tag is None]
    assert outside.parent_run_id == judge.run_id


def test_the_coder_gets_the_ticket_and_its_dependencies(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    items = WorkItems(trace)
    dep = items.add(_ticket(title="Widgets exist"))
    items.move(dep.id, "ready", "ready")
    items.move(dep.id, "in_progress", "started", actor="human")
    items.move(dep.id, "in_review", "reviewed", actor="human")
    items.move(dep.id, "done", "merged", actor="human")
    item = items.add(_ticket(), depends_on=[dep.id])
    items.ready()
    runner = BuildRunner()
    _build(agents, repo, trace, tmp_path, item.id, runner=runner)

    ((call),) = runner.coder_calls()
    assert call["payload"]["depends_on"] == [f"{dep.id}: Widgets exist"]
    assert call["payload"]["ticket"]["title"] == "Widgets report their size"
    assert call["payload"]["repo_path"] == str(call["repo_path"])
    assert "parent_run_id" in call and call["parent_run_id"] is None


def test_a_second_round_with_the_findings_can_ship(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    runner = BuildRunner([_judgement(BLOCKING), _judgement(SHIPPING)])
    result = _build(agents, repo, trace, tmp_path, item_id, runner=runner)

    assert result.code == ExitCode.OK
    first, second = result.document["rounds"]
    assert not first["panel"]["ship"] and second["panel"]["ship"]
    assert second["files_changed"] == [{"path": "widgets.py", "action": "modified"}]
    assert result.document["pull_request"]["commit"] == second["commit"]
    coder_one, coder_two = runner.coder_calls()
    assert "## Review findings to address" not in coder_one["payload"]["ticket"]["body"]
    body = coder_two["payload"]["ticket"]["body"]
    assert "## Review findings to address" in body and "shop/shipping.py:12-12" in body
    judge_rows = [
        r for r in trace.pipeline_runs(RUN_ID) if r.agent_ref.startswith("judge@") and r.tag is None
    ]
    assert coder_two["parent_run_id"] in {r.run_id for r in judge_rows}
    assert result.document["work_item"]["status_after"] == "in_review"


def test_findings_ticket_lists_each_finding_and_keeps_the_rest() -> None:
    finding = _finding("a.py", 3, 4, "minor") | {"suggested_fix": "Use <=."}
    ticket = findings_ticket(_ticket(), [finding])
    assert ticket.body.startswith("Add `size()` to widgets.py.\n\n## Review findings to address")
    assert "- [minor] a.py:3-4 Problem: It is wrong.\n  Suggested fix: Use <=." in ticket.body
    assert ticket.acceptance == _ticket().acceptance
    assert "without a finding" in findings_ticket(_ticket(), []).body


# --- runs that stop: the item goes to blocked


def _stopped(result: BuildResult, trace: Trace, item_id: str, message: str) -> dict[str, Any]:
    assert result.code == ExitCode.ERROR
    doc = result.document
    assert doc["outcome"] == "stopped" and doc["pull_request"] is None
    assert message in doc["stopped"]["message"]
    item = WorkItems(trace).get(item_id)
    assert item.status == "blocked" == doc["work_item"]["status_after"]
    (last,) = WorkItems(trace).events(item_id)[-1:]
    assert last.actor == ACTOR and last.pipeline_run_id == RUN_ID
    assert last.reason.startswith(f"{doc['stopped']['type']}: {doc['stopped']['message']}")
    assert doc["report_path"] in last.reason
    assert Path(doc["report_path"]).is_file()
    return doc


def test_a_second_request_for_changes_blocks_the_item(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    runner = BuildRunner([_judgement(BLOCKING)])
    result = _build(agents, repo, trace, tmp_path, item_id, runner=runner)
    doc = _stopped(result, trace, item_id, "the panel asked for changes 2 times")
    assert doc["stopped"]["type"] == "ChangesRequestedError"
    assert len(doc["rounds"]) == 2 and len(runner.coder_calls()) == 2
    assert gh.writes() == [] and _branches(repo.bare) == ""


def test_a_blocked_coder_blocks_the_item(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    sandbox = FakeSandbox()
    runner = BuildRunner(blocked="The ticket names no module.")
    result = _build(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox)
    doc = _stopped(result, trace, item_id, f"the coder is blocked on {item_id}")
    assert doc["stopped"]["hint"] == "The ticket names no module."
    assert doc["rounds"][0]["coder"]["output"]["blocked"] is True
    assert sandbox.runs == []


def test_a_failing_suite_blocks_the_item_before_the_panel(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    runner = BuildRunner()
    sandbox = FakeSandbox(exit_code=1, output=FAILED)
    result = _build(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox)
    doc = _stopped(result, trace, item_id, "the test suite failed in the sandbox with exit code 1")
    assert doc["rounds"][0]["suite"]["output_tail"] == FAILED.decode()
    assert doc["rounds"][0]["panel"] is None
    assert [call["spec"].name for call in runner.calls] == [FEATURE_CODER[0]]


def test_a_coder_that_changes_nothing_blocks_the_item(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    sandbox = FakeSandbox()
    result = _build(
        agents, repo, trace, tmp_path, item_id, runner=BuildRunner(rounds=[]), sandbox=sandbox
    )
    _stopped(result, trace, item_id, "the coder changed nothing in round 1")
    assert sandbox.runs == []


def test_a_run_past_its_budget_blocks_the_item(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    sandbox = FakeSandbox()
    result = _build(agents, repo, trace, tmp_path, item_id, sandbox=sandbox, budget=Budget(0.1))
    doc = _stopped(result, trace, item_id, "budget")
    assert doc["stopped"]["type"] == "BudgetExceeded"
    assert sandbox.runs == [] and len(sandbox.cleaned) == 1
    assert list(_work_dir(tmp_path).iterdir()) == []


@pytest.mark.parametrize("path", ["pyproject.toml", ".github/workflows/ci.yml", "pytest.py"])
def test_the_coder_writing_a_denied_path_through_its_tools_is_refused(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, path: str
) -> None:
    item_id = _ready(trace)
    runner = BuildRunner(rounds=[{path: "x = 1\n", **CODE}])
    result = _build(agents, repo, trace, tmp_path, item_id, runner=runner)
    doc = _stopped(result, trace, item_id, f"the coder may not change {path}")
    assert doc["stopped"]["type"] == "FileWriteRefusedError"
    ((agent, run_id, cost, decision, error),) = _refusals(trace)
    assert (agent, run_id, cost) == ("feature-coder@0.1.0", RUN_ID, 0.0)
    assert json.loads(decision)["check"] == "hook" and path in error


def test_a_denied_path_changed_by_another_route_is_refused_after_the_coder(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    sandbox = FakeSandbox()
    runner = BuildRunner(unhooked={"pytest.ini": "[pytest]\naddopts = -p no:cacheprovider\n"})
    result = _build(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox)
    _stopped(result, trace, item_id, "the coder may not change pytest.ini")
    ((_, _, _, decision, _),) = _refusals(trace)
    assert json.loads(decision)["check"] != "hook"
    assert sandbox.runs == []


def test_an_unexpected_error_blocks_the_item_and_is_raised(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    class Broken(FakeSandbox):
        def run(self, root: Path, argv: Sequence[str]) -> Any:
            raise RuntimeError("the sandbox fell over")

    item_id = _ready(trace)
    with pytest.raises(RuntimeError, match="fell over"):
        _build(agents, repo, trace, tmp_path, item_id, sandbox=Broken())
    assert WorkItems(trace).get(item_id).status == "blocked"
    doc = json.loads((tmp_path / "out" / f"{RUN_ID}.json").read_text(encoding="utf-8"))
    assert doc["stopped"]["type"] == "RuntimeError"


# --- the dry run


def test_a_dry_run_links_the_run_moves_nothing_and_writes_nothing(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    result = _build(agents, repo, trace, tmp_path, item_id, dry_run=True)

    assert result.code == ExitCode.OK
    doc = result.document
    assert doc["outcome"] == "dry_run" and doc["dry_run"] is True
    assert doc["pull_request"]["url"] is None
    assert doc["pull_request"]["outcome"] == "dry_run"
    assert doc["pull_request"]["branch"] == f"factory/{RUN_ID}"
    item = WorkItems(trace).get(item_id)
    assert item.status == "ready" == doc["work_item"]["status_after"]
    assert item.pipeline_run_ids == (RUN_ID,)
    assert all(e.actor != ACTOR for e in WorkItems(trace).events(item_id))
    assert gh.writes() == [] and _branches(repo.bare) == ""
    assert "dry run: would open a pull request from factory/" in result.text()


def test_a_dry_run_that_stops_leaves_the_item_ready(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    sandbox = FakeSandbox(exit_code=2, output=FAILED)
    result = _build(agents, repo, trace, tmp_path, item_id, sandbox=sandbox, dry_run=True)
    assert result.code == ExitCode.ERROR and result.document["outcome"] == "stopped"
    assert WorkItems(trace).get(item_id).status == "ready"


# --- preflight: nothing is touched


def _untouched(trace: Trace, item_id: str, runner: BuildRunner) -> None:
    item = WorkItems(trace).get(item_id)
    assert item.status == "ready" and item.pipeline_run_ids == ()
    assert runner.calls == []


def test_a_dirty_repository_is_refused(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    (repo.root / "notes.txt").write_text("draft\n", encoding="utf-8")
    runner = BuildRunner()
    with pytest.raises(DirtyRepositoryError, match="notes.txt"):
        _build(agents, repo, trace, tmp_path, item_id, runner=runner)
    _untouched(trace, item_id, runner)


def test_a_repository_without_a_github_origin_is_refused(
    agents: BuildAgents, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    repo = github_repo(tmp_path, origin=None)
    item_id = _ready(trace)
    runner = BuildRunner()
    with pytest.raises(ToolInputError):
        _build(agents, repo, trace, tmp_path, item_id, runner=runner)
    _untouched(trace, item_id, runner)


def test_an_origin_that_is_not_the_tickets_repository_is_refused(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace, _ticket(repo="acme/gadgets"))
    runner = BuildRunner()
    with pytest.raises(UsageError, match="origin is acme/widgets, the ticket's repository is"):
        _build(agents, repo, trace, tmp_path, item_id, runner=runner)
    _untouched(trace, item_id, runner)


def test_a_repo_option_that_is_not_the_tickets_path_is_refused(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "commit", "-q", "--allow-empty", "-m", "empty")
    item_id = _ready(trace, _ticket(repo=str(repo.root)))
    runner = BuildRunner()
    with pytest.raises(UsageError, match="is not the ticket's repository"):
        _build(agents, repo, trace, tmp_path, item_id, runner=runner, repo_path=other)
    _untouched(trace, item_id, runner)


def test_a_ticket_naming_a_path_builds_there_without_repo(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace, _ticket(repo=str(repo.root)))
    result = _build(agents, repo, trace, tmp_path, item_id, repo_path=None)
    assert result.code == ExitCode.OK
    assert result.document["repo_path"] == str(repo.root.resolve())


def test_a_work_dir_with_a_colon_is_a_usage_error(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    work = tmp_path / "a:b"
    work.mkdir()
    runner = BuildRunner()
    with pytest.raises(UsageError, match="has a ':' in its path"):
        _build(agents, repo, trace, tmp_path, item_id, runner=runner, work_dir=work)
    _untouched(trace, item_id, runner)


def test_the_sandbox_is_checked_before_the_item_moves(
    agents: BuildAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    class Missing(FakeSandbox):
        def check(self) -> None:
            raise UsageError("no docker")

    item_id = _ready(trace)
    runner = BuildRunner()
    with pytest.raises(UsageError, match="no docker"):
        _build(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=Missing())
    _untouched(trace, item_id, runner)


# --- factory build


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: FakeGh) -> BuildRunner:
    """Point `factory build` at fakes: the runners, the sandbox, the trace database and the keys."""
    runner = BuildRunner()
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(build_loop, "run", runner)
    monkeypatch.setattr(build_loop, "run_jev", FakeJev())
    monkeypatch.setattr(build_loop, "DockerSandbox", FakeSandbox)
    return runner


def _cli_item(tmp_path: Path, ticket: Ticket | None = None, *, ready: bool = True) -> str:
    with Trace(tmp_path / "traces.duckdb") as trace:
        items = WorkItems(trace)
        item = items.add(ticket or _ticket())
        if ready:
            items.ready()
        return item.id


def _argv(item_id: str, repo: GithubRepo, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "build",
        item_id,
        "--repo",
        str(repo.root),
        "--out-dir",
        str(tmp_path / "out"),
        "--work-dir",
        str(_work_dir(tmp_path)),
        "--allow-unpublished",
        *extra,
    ]


def test_factory_build_prints_the_report_and_exits_0(
    cli: BuildRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = _cli_item(tmp_path)
    assert main(_argv(item_id, repo, tmp_path, "--json")) == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["outcome"] == "shipped"
    assert Path(document["report_path"]).is_file()
    assert "build run" in captured.err and f"pull request {PULL_JSON['html_url']}" in captured.err


def test_factory_build_prints_text_without_json(
    cli: BuildRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = _cli_item(tmp_path)
    assert main(_argv(item_id, repo, tmp_path, "--dry-run")) == ExitCode.OK
    out = capsys.readouterr().out
    assert "round 1: 2 files changed; suite passed in 0.1s; panel ships" in out
    assert f"work item {item_id}: ready -> ready" in out


def test_factory_build_exits_1_when_the_panel_asks_twice(
    cli: BuildRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.judgements = [_judgement(BLOCKING)]
    item_id = _cli_item(tmp_path)
    assert main(_argv(item_id, repo, tmp_path)) == ExitCode.ERROR
    out = capsys.readouterr().out
    assert "stopped: the panel asked for changes 2 times" in out
    assert f"work item {item_id}: ready -> blocked" in out


def test_factory_build_exits_2_for_an_item_that_is_not_ready(
    cli: BuildRunner, repo: GithubRepo, tmp_path: Path
) -> None:
    item_id = _cli_item(tmp_path, ready=False)
    assert main(_argv(item_id, repo, tmp_path)) == ExitCode.USAGE
    assert cli.calls == []


def test_factory_build_exits_2_for_an_incident(
    cli: BuildRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = _cli_item(tmp_path, _ticket(kind="incident"))
    assert main(_argv(item_id, repo, tmp_path)) == ExitCode.USAGE
    assert "factory hotfix" in capsys.readouterr().err


def test_factory_build_exits_3_for_an_unknown_item(
    cli: BuildRunner, repo: GithubRepo, tmp_path: Path
) -> None:
    assert main(_argv("wi-42", repo, tmp_path)) == ExitCode.NOT_FOUND


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "GH_TOKEN"])
def test_factory_build_exits_4_without_a_key(
    cli: BuildRunner,
    repo: GithubRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    item_id = _cli_item(tmp_path)
    monkeypatch.delenv(key)
    assert main(_argv(item_id, repo, tmp_path)) == ExitCode.AUTH_OR_CONFIG
    assert f"{key} is not set" in capsys.readouterr().err
    assert cli.calls == []
    with Trace(tmp_path / "traces.duckdb") as trace:
        assert WorkItems(trace).get(item_id).status == "ready"
