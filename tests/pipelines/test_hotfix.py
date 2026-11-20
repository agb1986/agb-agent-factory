"""Tests for pipelines/hotfix.py: a reproduced incident ticket to a `hotfix` pull request.

No Docker, no API key and no network. The ticket is the one `factory incident` files in
tests/golden/incident.json, pointed at a work tree whose pushes land in a bare repository under
`tmp_path` (tests/gh_fake.py). The fixer, the reviewers and the judge are the fakes of
tests/pipelines/test_fix_loop.py, and the sandbox answers the reproduction run and then the suite.
The escalation-summary agent that runs after a stop is faked by `HotfixRunner`.
"""

import dataclasses
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.cli import main
from factory.exit import ExitCode, UsageError
from pipelines import hotfix
from pipelines.hotfix import (
    ACTOR,
    LABEL,
    HotfixAgents,
    HotfixResult,
    check_hotfixable,
    event_time,
    hotfix_finding,
    resolve_hotfix_agents,
    run_hotfix,
)
from registry.common_schemas import Ticket
from registry.spec import LLMSpec
from runtime.after_escalation import SUMMARY_AGENT
from runtime.budget import Budget, BudgetExceeded
from runtime.escalations import Escalation, InvalidEscalation, get_summary, list_escalations
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItemNotFound, WorkItems
from tests.gh_fake import FakeGh, GithubRepo, git, github_repo
from tests.pipelines.test_build_loop import UNGROUNDED, _grounded_summary
from tests.pipelines.test_fix_loop import PASSED, FakeSandbox, FixRunner
from tests.pipelines.test_incident import ASSERTION, REPRODUCED, TEST_CODE, TEST_PATH
from tests.pipelines.test_review_panel import BLOCKING, FakeJev, _judgement
from tools.sandbox import SandboxResult

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "pipeline-run-1"
FULL = "acme/widgets"
REPO_JSON = {"full_name": FULL, "fork": False, "default_branch": "main"}
PULL_JSON = {"number": 7, "html_url": f"https://github.com/{FULL}/pull/7"}
LABELS = f"repos/{FULL}/labels?per_page=100"
SCORES = "reviews/scores.py"
SOURCES = {
    "reviews/__init__.py": "",
    SCORES: "def average_rating(ratings: list[int]) -> float:\n"
    "    total = sum(ratings)\n"
    "    return round(total / len(ratings), 1)\n",
    "reviews/listing.py": "from reviews.scores import average_rating\n\n\n"
    "def product_line(name: str, ratings: list[int]) -> str:\n"
    '    return f"{name}: {average_rating(ratings)}"\n',
}
FIXED = (
    "def average_rating(ratings: list[int]) -> float | None:\n"
    "    if not ratings:\n"
    "        return None\n"
    "    return round(sum(ratings) / len(ratings), 1)\n"
)
FAILED = b"1 failed in 0.02s\n"


def _golden_ticket() -> dict[str, Any]:
    document = json.loads((ROOT / "tests" / "golden" / "incident.json").read_text("utf-8"))
    return document["ticket"]


def hotfix_ticket(**changes: Any) -> Ticket:
    """Return the ticket of tests/golden/incident.json for `acme/widgets` at `main`, with `changes`."""
    golden = _golden_ticket()
    fields = {key: golden[key] for key in Ticket.model_fields if key in golden}
    fields |= {"repo": FULL, "ref": "main"}
    return Ticket.model_validate(fields | changes)


def _body(old: str, new: str) -> str:
    body = _golden_ticket()["body"]
    assert old in body
    return body.replace(old, new)


class SequencedSandbox(FakeSandbox):
    """`FakeSandbox` answering `results` in turn: the reproduction run, then the suite."""

    def __init__(self, *results: tuple[int, bytes]) -> None:
        super().__init__()
        self.results = list(results) or [(1, REPRODUCED), (0, PASSED)]

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        self.runs.append((root, tuple(argv), root.is_dir()))
        exit_code, output = self.results[min(len(self.runs), len(self.results)) - 1]
        return SandboxResult(tuple(argv), exit_code, output, 0.1)


class HotfixRunner(FixRunner):
    """`FixRunner` that also answers the escalation-summary agent.

    The summary is `summary` when given, raised when it is an exception, and otherwise one that
    quotes the record's question. The call traces a row and charges `0.25`, as the others do.
    """

    def __init__(
        self,
        judgement: Mapping[str, Any] | None = None,
        *,
        summary: Mapping[str, Any] | Exception | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(judgement, **kwargs)
        self.summary = summary

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any] | BaseModel, trace: Trace, **kwargs: Any
    ) -> BaseModel:
        if spec.name != SUMMARY_AGENT[0]:
            return super().__call__(spec, payload, trace, **kwargs)
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        self.calls.append({"spec": spec, "payload": data, **kwargs})
        trace.record(
            TraceRow(
                run_id=kwargs["run_id"],
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=data,
                cost_usd=0.25,
            )
        )
        kwargs["budget"].charge(0.25)
        if isinstance(self.summary, Exception):
            raise self.summary
        output = _grounded_summary(data) if self.summary is None else dict(self.summary)
        return spec.output_class.model_validate(output)


def _runner(judgement: dict[str, Any] | None = None, **kwargs: Any) -> HotfixRunner:
    kwargs.setdefault("fixes", {SCORES: FIXED})
    return HotfixRunner(judgement, tests={}, **kwargs)


@pytest.fixture(scope="module")
def agents() -> HotfixAgents:
    return resolve_hotfix_agents(allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    return route_gh(FakeGh(tmp_path, monkeypatch))


def route_gh(gh: FakeGh) -> FakeGh:
    """Answer the reads and writes of a hotfix on `acme/widgets`, whose labels include `hotfix`."""
    gh.route("GET", f"repos/{FULL}", REPO_JSON)
    gh.route("GET", LABELS, [{"name": "bug"}, {"name": LABEL}])
    gh.route("POST", f"repos/{FULL}/pulls", PULL_JSON)
    gh.route("POST", f"repos/{FULL}/issues/7/labels", [{"name": LABEL}])
    return gh


def hotfix_repo(tmp_path: Path) -> GithubRepo:
    """Return `github_repo(tmp_path)` with the `reviews` package the incident ticket names."""
    made = github_repo(tmp_path)
    for path, content in SOURCES.items():
        target = made.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(made.root, "add", "-A")
    git(made.root, "commit", "-q", "-m", "reviews")
    return GithubRepo(made.root, made.bare, git(made.root, "rev-parse", "HEAD"))


@pytest.fixture
def repo(tmp_path: Path, gh: FakeGh) -> GithubRepo:
    return hotfix_repo(tmp_path)


def _ready(trace: Trace, ticket: Ticket | None = None) -> str:
    items = WorkItems(trace)
    item = items.add(ticket or hotfix_ticket())
    items.ready()
    return item.id


def _work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def _hotfix(
    agents: HotfixAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    item_id: str,
    *,
    runner: HotfixRunner | None = None,
    sandbox: FakeSandbox | None = None,
    **kwargs: Any,
) -> HotfixResult:
    return run_hotfix(
        agents,
        item_id,
        trace,
        repo_path=repo.root,
        out_dir=tmp_path / "out",
        work_dir=_work_dir(tmp_path),
        sandbox=sandbox or SequencedSandbox(),
        runner=runner or _runner(),
        jev_runner=FakeJev(),
        pipeline_run_id=RUN_ID,
        **kwargs,
    )


def _branches_tags_and_head(root: Path) -> tuple[str, str]:
    return git(root, "for-each-ref", "refs/heads", "refs/tags"), git(root, "rev-parse", "HEAD")


def _stopped(
    result: HotfixResult, trace: Trace, item_id: str, message: str, *, status: str = "escalated"
) -> dict[str, Any]:
    assert result.code == ExitCode.ERROR
    doc = result.document
    assert doc["outcome"] == "stopped" and doc["pull_request"] is None
    assert message in doc["stopped"]["message"]
    item = WorkItems(trace).get(item_id)
    assert item.status == status == doc["work_item"]["status_after"]
    (last,) = WorkItems(trace).events(item_id)[-1:]
    assert last.actor == ACTOR and last.pipeline_run_id == RUN_ID
    assert last.reason.startswith(f"{doc['stopped']['type']}: {doc['stopped']['message']}")
    assert last.reason.endswith(f"; report {doc['report_path']}")
    assert Path(doc["report_path"]).is_file()
    assert doc["timing"]["pr_opened_at"] is None and doc["timing"]["time_to_pr_seconds"] is None
    return doc


def _escalation(trace: Trace, doc: dict[str, Any], trigger: str) -> Escalation:
    """Check the stop wrote one record with `trigger`, and return it."""
    (record,) = list_escalations(trace, work_item_id=doc["work_item"]["id"])
    assert doc["escalation"] == record.id and doc["board_error"] is None
    assert record.trigger == trigger and record.actor == ACTOR
    assert record.pipeline_run_id == RUN_ID and record.from_status == "in_progress"
    assert record.question.startswith(f"{doc['stopped']['message']}.")
    return record


# --- reading the incident ticket


def test_the_agents_resolve_to_the_guarded_fixer_and_the_full_panel(agents: HotfixAgents) -> None:
    assert agents.fixer.address.startswith("fixer@")
    assert agents.fixer.sandbox_profile == "offline"
    assert agents.fixer.guard is not None and agents.fixer.files is not None
    assert agents.panel.triage is not None and agents.panel.gate is not None


def test_the_ticket_factory_incident_files_is_hotfixable(trace: Trace) -> None:
    plan = check_hotfixable(WorkItems(trace).get(_ready(trace)))
    assert plan.code == ((TEST_PATH, TEST_CODE),)
    assert plan.test_files == (TEST_PATH,)
    assert (plan.exception, plan.crash_file, plan.level) == (
        "ZeroDivisionError",
        SCORES,
        "error",
    )
    assert plan.culprit == (
        SCORES,
        6,
        7,
        "it divides by the number of ratings without checking for none.",
    )
    assert (plan.first_seen, plan.last_seen) == ("2026-11-18T09:12:44Z", "2026-11-20T15:02:10Z")


def test_a_ticket_of_another_kind_is_sent_to_factory_build(trace: Trace) -> None:
    item_id = _ready(trace, hotfix_ticket(kind="bug"))
    with pytest.raises(UsageError, match="takes only incidents") as info:
        check_hotfixable(WorkItems(trace).get(item_id))
    assert "factory build" in (info.value.hint or "")


def test_an_item_that_is_not_ready_is_refused(trace: Trace) -> None:
    item = WorkItems(trace).add(hotfix_ticket())
    with pytest.raises(UsageError, match="is todo, and the hotfix pipeline takes only ready"):
        check_hotfixable(item)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("## Culprit", "## Blame", "does not have an incident body"),
        ("- Reproduced: yes", "- Reproduced: no", "was not reproduced"),
        (f"`{TEST_PATH}`:\n", "", "carries no test code"),
        ('no ratings"\n```', 'no ratings"', "has no closing fence"),
        (f"`{TEST_PATH}`:\n", "`../escape.py`:\n", "unsafe path: ../escape.py"),
        (f"`{TEST_PATH}`:\n", "`.git/hooks/test_x.py`:\n", "unsafe path"),
        ("- Crash file: reviews/scores.py", "- Crash file: unknown", "crash file of its event"),
        ("`reviews/scores.py` lines 6-7:", "Somewhere in scores:", "Culprit section"),
    ],
)
def test_a_body_the_pipeline_cannot_act_on_is_refused(
    trace: Trace, old: str, new: str, message: str
) -> None:
    item_id = _ready(trace, hotfix_ticket(body=_body(old, new)))
    with pytest.raises(UsageError, match=message):
        check_hotfixable(WorkItems(trace).get(item_id))


def test_the_finding_is_the_culprit_with_the_test_as_its_proof(trace: Trace) -> None:
    ticket = hotfix_ticket()
    plan = check_hotfixable(WorkItems(trace).get(_ready(trace, ticket)))
    finding = hotfix_finding(ticket, plan)
    assert (finding.file, finding.line_start, finding.line_end) == (SCORES, 6, 7)
    assert (finding.severity, finding.category) == ("major", "crash")
    assert finding.title == ticket.title
    assert TEST_CODE.strip() in finding.body
    assert "by changing the code, not the test" in finding.body


def test_a_fatal_event_is_a_blocker(trace: Trace) -> None:
    ticket = hotfix_ticket(body=_body("- Level: error", "- Level: fatal"))
    plan = check_hotfixable(WorkItems(trace).get(_ready(trace, ticket)))
    assert hotfix_finding(ticket, plan).severity == "blocker"


@pytest.mark.parametrize(
    ("events", "when", "source"),
    [
        (
            "first seen 2026-11-18T09:12:44Z, last seen 2026-11-20T15:02:10Z",
            datetime(2026, 11, 20, 15, 2, 10, tzinfo=UTC),
            "last_seen",
        ),
        (
            "first seen 2026-11-18T09:12:44",
            datetime(2026, 11, 18, 9, 12, 44, tzinfo=UTC),
            "first_seen",
        ),
        ("first seen yesterday, last seen today", None, None),
        ("no times at all", None, None),
    ],
)
def test_the_event_time_is_the_last_time_seen(
    trace: Trace, events: str, when: datetime | None, source: str | None
) -> None:
    body = _body("first seen 2026-11-18T09:12:44Z, last seen 2026-11-20T15:02:10Z", events)
    plan = check_hotfixable(WorkItems(trace).get(_ready(trace, hotfix_ticket(body=body))))
    assert event_time(plan) == (when, source)


# --- a run that ships


def test_a_shipped_hotfix_opens_a_labelled_pull_request_and_moves_the_item_to_in_review(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    runner, sandbox = _runner(), SequencedSandbox()
    before = _branches_tags_and_head(repo.root)
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox)

    assert result.code == ExitCode.OK
    doc = result.to_json()
    assert doc["outcome"] == "shipped" and doc["stopped"] is None
    assert doc["work_item"]["status_before"] == "ready"
    assert doc["work_item"]["status_after"] == "in_review"
    assert doc["head"] == repo.head
    assert doc["reproduction"]["reproduced"] is True
    assert doc["fix"]["files_changed"] == [
        {"path": SCORES, "action": "modified", "by": "fixer"},
        {"path": TEST_PATH, "action": "created", "by": "reproduction"},
    ]
    assert doc["fix"]["suite"]["passed"] and doc["fix"]["panel"]["ship"]

    # The reproduction runs the test alone and fails; the suite runs it all and passes.
    (_, repro, _), (_, suite, _) = sandbox.runs
    assert repro[-1] == TEST_PATH and TEST_PATH not in suite
    assert runner.names().count("fixer") == 1

    pull = doc["pull_request"]
    assert pull["url"] == PULL_JSON["html_url"] and pull["branch"] == f"factory/{RUN_ID}"
    assert pull["labels"] == [LABEL]
    assert git(repo.bare, "rev-parse", f"refs/heads/factory/{RUN_ID}") == pull["commit"]
    assert git(repo.bare, "show", f"{pull['commit']}:{SCORES}") == FIXED.strip()
    assert git(repo.bare, "show", f"{pull['commit']}:{TEST_PATH}") == TEST_CODE.strip()
    post, labels = gh.writes()
    assert post.path == f"repos/{FULL}/pulls" and post.data["base"] == "main"
    assert "## Acceptance\n\n- tests/test_no_ratings.py passes" in post.data["body"]
    assert "A person merges and deploys this; the factory does not." in post.data["body"]
    assert labels.path == f"repos/{FULL}/issues/7/labels"
    assert labels.data == {"labels": [LABEL]}
    assert not any("merge" in call.path for call in gh.calls())

    # Remote-tracking refs may change with the push, so only branches and tags are compared.
    assert _branches_tags_and_head(repo.root) == before
    assert git(repo.root, "status", "--porcelain") == ""
    assert list(_work_dir(tmp_path).iterdir()) == []
    assert sandbox.checked == 1 and len(sandbox.cleaned) == 1

    timing = doc["timing"]
    assert timing["event_time"] == "2026-11-20T15:02:10+00:00"
    assert timing["event_time_source"] == "last_seen"
    opened = datetime.fromisoformat(timing["pr_opened_at"])
    assert timing["time_to_pr_seconds"] == round(
        (opened - datetime(2026, 11, 20, 15, 2, 10, tzinfo=UTC)).total_seconds(), 3
    )
    report = json.loads((tmp_path / "out" / f"{RUN_ID}.json").read_text(encoding="utf-8"))
    assert report == json.loads(json.dumps(doc))
    assert "label hotfix; a person merges and deploys" in result.text()
    assert "time from event to pull request" in result.text()


def test_every_move_and_row_carries_the_run_and_the_write_is_the_judges(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    runner = _runner()
    _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner)

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
    assert outside.agent_ref == agents.panel.judge.address
    assert outside.parent_run_id == judge.run_id


def test_the_fixer_may_not_change_the_reproduction_test(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    runner = _runner(fixes={SCORES: FIXED}, unhooked={TEST_PATH: "def test_x() -> None:\n"})
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner)
    doc = _stopped(result, trace, item_id, TEST_PATH)
    assert gh.writes() == []
    _escalation(trace, doc, "check_failed")


# --- dry runs


def test_a_dry_run_reads_the_labels_but_writes_and_moves_nothing(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    result = _hotfix(agents, repo, trace, tmp_path, item_id, dry_run=True)

    assert result.code == ExitCode.OK
    doc = result.document
    assert doc["outcome"] == "dry_run" and doc["dry_run"] is True
    assert doc["pull_request"]["url"] is None
    assert doc["pull_request"]["outcome"] == "dry_run"
    assert doc["timing"]["time_to_pr_seconds"] is None
    item = WorkItems(trace).get(item_id)
    assert item.status == "ready" == doc["work_item"]["status_after"]
    assert item.pipeline_run_ids == (RUN_ID,)
    assert all(e.actor != ACTOR for e in WorkItems(trace).events(item_id))
    assert gh.writes() == [] and git(repo.bare, "for-each-ref") == ""
    assert any(call.path == LABELS for call in gh.calls())
    assert "dry run: would open a pull request from factory/" in result.text()


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_missing_hotfix_label_stops_the_run_with_exit_1(
    agents: HotfixAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    gh: FakeGh,
    dry_run: bool,
) -> None:
    gh.route("GET", LABELS, [{"name": "bug"}])
    item_id = _ready(trace)
    result = _hotfix(agents, repo, trace, tmp_path, item_id, dry_run=dry_run)

    assert result.code == ExitCode.ERROR
    doc = result.document
    assert doc["stopped"]["type"] == "OutsideWriteRefused"
    assert "labels missing on acme/widgets: hotfix" in doc["stopped"]["message"]
    assert "never creates a label" in doc["stopped"]["hint"]
    assert gh.writes() == []
    if dry_run:
        assert WorkItems(trace).get(item_id).status == "ready"
    else:
        # An operator precondition, outside the item: blocked, not escalated.
        doc = _stopped(result, trace, item_id, "labels missing", status="blocked")
        assert doc["escalation"] is None and list_escalations(trace) == []


# --- runs that stop


def test_a_test_that_no_longer_reproduces_stops_before_any_model_runs(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    runner = _runner()
    sandbox = SequencedSandbox((1, ASSERTION))
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner, sandbox=sandbox)

    doc = _stopped(result, trace, item_id, "the reproduction test no longer fails")
    assert doc["stopped"]["type"] == "HotfixError"
    assert doc["reproduction"]["reproduced"] is False
    assert doc["reproduction"]["commit"] is not None and doc["fix"] is None
    # The only model run is the summary of the escalation, after the stop.
    assert runner.names() == [SUMMARY_AGENT[0]] and gh.writes() == []
    assert "does not reproduce" in result.text()
    record = _escalation(trace, doc, "contradiction")
    assert "factory incident" in record.question
    assert TEST_PATH in record.diff and record.agent_outputs == ()
    assert record.suite_tail == doc["reproduction"]["run"]["output_tail"]
    assert f"escalation {record.id}" in result.text()


def test_a_request_for_changes_escalates_the_item_after_one_round(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    runner = _runner(_judgement(BLOCKING))
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner)

    doc = _stopped(result, trace, item_id, "a hotfix gets one round")
    assert doc["stopped"]["type"] == "HotfixChangesRequestedError"
    assert doc["fix"]["panel"]["ship"] is False
    assert runner.names().count("fixer") == 1 and gh.writes() == []
    assert "panel asks for changes" in result.text()
    record = _escalation(trace, doc, "changes_requested")
    assert [o.agent for o in record.agent_outputs] == [agents.fixer.address, "review panel"]
    assert record.agent_outputs[0].run_id == doc["fix"]["fixer"]["run_id"]
    assert SCORES in record.diff and TEST_PATH in record.diff
    assert record.suite_tail == doc["fix"]["suite"]["output_tail"]


def test_a_failing_suite_escalates_the_item(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    sandbox = SequencedSandbox((1, REPRODUCED), (1, FAILED))
    result = _hotfix(agents, repo, trace, tmp_path, item_id, sandbox=sandbox)
    doc = _stopped(result, trace, item_id, "suite failed")
    assert gh.writes() == []
    record = _escalation(trace, doc, "check_failed")
    assert record.suite_tail and record.suite_tail == doc["stopped"]["hint"]
    assert "get past this check" in record.question and SCORES in record.diff


def test_a_fixer_that_changes_nothing_escalates_the_item(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=_runner(fixes={}))
    doc = _stopped(result, trace, item_id, "changed nothing")
    assert gh.writes() == []
    record = _escalation(trace, doc, "contradiction")
    assert "already fixed" in record.question


def test_a_run_past_its_budget_escalates_the_item(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path, gh: FakeGh
) -> None:
    item_id = _ready(trace)
    result = _hotfix(agents, repo, trace, tmp_path, item_id, budget=Budget(0.0001))
    doc = _stopped(result, trace, item_id, "budget")
    assert doc["stopped"]["type"] == BudgetExceeded.__name__ and gh.writes() == []
    record = _escalation(trace, doc, "budget")
    assert "bigger budget" in record.question


def test_a_stop_whose_record_cannot_be_written_blocks_the_item(
    agents: HotfixAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(*args: Any, **kwargs: Any) -> Any:
        raise InvalidEscalation("no room for a record")

    monkeypatch.setattr(hotfix, "escalate", refused)
    item_id = _ready(trace)
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=_runner(fixes={}))
    doc = _stopped(result, trace, item_id, "changed nothing", status="blocked")
    assert doc["escalation"] is None and doc["after_escalation"] is None
    assert doc["board_error"]["message"] == "no room for a record"


# --- after the escalation: the summary step never changes how the hotfix ends


def _summary_step(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the report's summary step, after checking the report file carries the same one."""
    report = json.loads(Path(doc["report_path"]).read_text("utf-8"))
    assert report["after_escalation"] == doc["after_escalation"]
    return doc["after_escalation"]["summary"]


def test_a_hotfix_escalation_gets_a_grounded_summary_under_the_runs_budget(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    assert agents.summary is not None and agents.summary.address == "escalation-summary@0.1.0"
    item_id = _ready(trace)
    runner = _runner(fixes={})
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner)
    doc = _stopped(result, trace, item_id, "changed nothing")
    record = _escalation(trace, doc, "contradiction")

    (call,) = [c for c in runner.calls if c["spec"].name == SUMMARY_AGENT[0]]
    assert call["payload"]["id"] == record.id and call["pipeline_run_id"] == RUN_ID
    assert _summary_step(doc) == {"done": True, "run_id": call["run_id"], "reason": None}
    stored = get_summary(trace, record.id)
    assert stored is not None and stored.run_id == call["run_id"]
    assert stored.agent_ref == agents.summary.address
    # The summary's run is in the pipeline run's cost: the fixer's 0.25 and its own 0.25.
    assert doc["total_cost_usd"] == pytest.approx(0.5)


def test_a_hotfix_budget_stop_gets_no_summary(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    runner = _runner()
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner, budget=Budget(0.0001))
    doc = _stopped(result, trace, item_id, "budget")
    record = _escalation(trace, doc, "budget")
    step = _summary_step(doc)
    assert step["done"] is False and step["run_id"] is None and "budget" in step["reason"]
    assert SUMMARY_AGENT[0] not in runner.names()
    assert get_summary(trace, record.id) is None


@pytest.mark.parametrize(
    ("summary", "reason"),
    [
        (UNGROUNDED, "the summary failed its check: "),
        (RuntimeError("the model is down"), "RuntimeError: the model is down"),
    ],
    ids=["ungrounded", "agent-error"],
)
def test_a_hotfix_summary_that_fails_leaves_the_escalation_as_it_was(
    agents: HotfixAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    summary: Mapping[str, Any] | Exception,
    reason: str,
) -> None:
    item_id = _ready(trace)
    runner = _runner(fixes={}, summary=summary)
    result = _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner)
    # The item stays escalated, the report keeps the record's id and the code stays the stop's.
    doc = _stopped(result, trace, item_id, "changed nothing")
    record = _escalation(trace, doc, "contradiction")
    assert result.code == ExitCode.ERROR
    step = _summary_step(doc)
    assert step["done"] is False and step["run_id"] is not None
    assert step["reason"].startswith(reason)
    assert get_summary(trace, record.id) is None


def test_a_hotfix_without_a_summary_agent_still_escalates(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    runner = _runner(fixes={})
    without = dataclasses.replace(agents, summary=None)
    result = _hotfix(without, repo, trace, tmp_path, item_id, runner=runner)
    doc = _stopped(result, trace, item_id, "changed nothing")
    record = _escalation(trace, doc, "contradiction")
    assert _summary_step(doc) == {
        "done": False,
        "run_id": None,
        "reason": "no published escalation-summary agent resolved",
    }
    assert SUMMARY_AGENT[0] not in runner.names()
    assert get_summary(trace, record.id) is None


def test_a_crash_blocks_the_item_and_is_raised(
    agents: HotfixAgents,
    repo: GithubRepo,
    trace: Trace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(hotfix, "fix_and_review", crash)
    item_id = _ready(trace)
    with pytest.raises(RuntimeError, match="boom"):
        _hotfix(agents, repo, trace, tmp_path, item_id)
    assert WorkItems(trace).get(item_id).status == "blocked"
    assert list_escalations(trace) == []


def test_a_dry_run_that_stops_leaves_the_item_ready(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace)
    sandbox = SequencedSandbox((1, ASSERTION))
    result = _hotfix(agents, repo, trace, tmp_path, item_id, sandbox=sandbox, dry_run=True)
    assert result.code == ExitCode.ERROR and result.document["outcome"] == "stopped"
    assert WorkItems(trace).get(item_id).status == "ready"


# --- preflight: nothing is touched


def test_an_unknown_item_is_not_found(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    with pytest.raises(WorkItemNotFound):
        _hotfix(agents, repo, trace, tmp_path, "wi-42")


def test_a_ticket_for_another_repository_is_refused(
    agents: HotfixAgents, repo: GithubRepo, trace: Trace, tmp_path: Path
) -> None:
    item_id = _ready(trace, hotfix_ticket(repo="acme/gadgets"))
    runner = _runner()
    with pytest.raises(UsageError, match="the ticket's repository is acme/gadgets"):
        _hotfix(agents, repo, trace, tmp_path, item_id, runner=runner)
    item = WorkItems(trace).get(item_id)
    assert item.status == "ready" and item.pipeline_run_ids == () and runner.calls == []


# --- factory hotfix


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: FakeGh) -> FixRunner:
    """Point `factory hotfix` at fakes: the runners, the sandbox, the trace database and the key."""
    runner = _runner()
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(hotfix, "run", runner)
    monkeypatch.setattr(hotfix, "run_jev", FakeJev())
    monkeypatch.setattr(hotfix, "DockerSandbox", SequencedSandbox)
    return runner


def _cli_item(tmp_path: Path, ticket: Ticket | None = None) -> str:
    with Trace(tmp_path / "traces.duckdb") as trace:
        return _ready(trace, ticket)


def _argv(item_id: str, repo: GithubRepo, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "hotfix",
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


def test_factory_hotfix_prints_the_report_and_exits_0(
    cli: FixRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = _cli_item(tmp_path)
    assert main(_argv(item_id, repo, tmp_path, "--json")) == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["outcome"] == "shipped"
    assert Path(document["report_path"]).is_file()
    assert "hotfix run" in captured.err and str(PULL_JSON["html_url"]) in captured.err


def test_factory_hotfix_prints_text_for_a_dry_run(
    cli: FixRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = _cli_item(tmp_path)
    assert main(_argv(item_id, repo, tmp_path, "--dry-run")) == ExitCode.OK
    out = capsys.readouterr().out
    assert f"reproduction {TEST_PATH} still reproduces" in out
    assert "fix: 2 files changed; suite passed in 0.1s; panel ships" in out
    assert f"work item {item_id}: ready -> ready" in out


def test_factory_hotfix_exits_1_when_the_panel_asks_for_changes(
    cli: FixRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.judgement = _judgement(BLOCKING)
    item_id = _cli_item(tmp_path)
    assert main(_argv(item_id, repo, tmp_path)) == ExitCode.ERROR
    out = capsys.readouterr().out
    assert "stopped: the panel asked for changes, and a hotfix gets one round" in out
    assert f"work item {item_id}: ready -> escalated" in out
    assert "escalation esc-1: factory escalations show to answer it" in out


def test_factory_hotfix_exits_2_for_a_featurehotfix_ticket(
    cli: FixRunner, repo: GithubRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    item_id = _cli_item(tmp_path, hotfix_ticket(kind="feature"))
    assert main(_argv(item_id, repo, tmp_path)) == ExitCode.USAGE
    assert "factory build" in capsys.readouterr().err
    assert cli.calls == []


def test_factory_hotfix_exits_3_for_an_unknown_item(
    cli: FixRunner, repo: GithubRepo, tmp_path: Path
) -> None:
    assert main(_argv("wi-42", repo, tmp_path)) == ExitCode.NOT_FOUND


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "GH_TOKEN"])
def test_factory_hotfix_exits_4_without_a_key(
    cli: FixRunner,
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
