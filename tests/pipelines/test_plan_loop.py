"""Tests for pipelines/plan_loop.py: the plan loop, its PRD file and `factory plan`.

No API key and no network. The two agents are a fake runner that answers with canned outputs and
records a trace row per call; the work-item store is a real DuckDB file in a temporary directory.
"""

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factory.cli import main
from factory.exit import ExitCode, UsageError
from fixtures.build import build
from pipelines import plan_loop
from pipelines.plan_loop import (
    ACTOR,
    ARCHITECT,
    AWAITING_ANSWERS,
    AWAITING_APPROVAL,
    BREAKDOWN,
    FILED,
    PlanAgents,
    PlanError,
    Prd,
    Question,
    apply_answers,
    parse_answer,
    parse_prd,
    plan_batch,
    prd_text,
    resolve_plan_agents,
    run_plan,
)
from registry.spec import LLMSpec
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItems

QUESTIONS = [
    {"question": "Which currency does it show?", "why_it_matters": "Sets the price format."},
    {"question": "Is a = b allowed in a name?", "why_it_matters": "Changes the parser."},
]


def _prd(questions: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "problem": "Shipping is quoted wrongly.",
        "goals": ["Quote shipping by weight"],
        "non_goals": ["Tax"],
        "user_stories": [
            {"as_a": "a customer", "i_want": "a right quote", "so_that": "I pay what I owe"}
        ],
        "constraints": ["Keep the public API"],
        "risks": ["Rounding"],
        "open_questions": [] if questions is None else questions,
        "related_files": ["shop/shipping.py"],
    }


def _ticket(title: str, paths: list[str], depends_on: list[int], size: str = "S") -> dict[str, Any]:
    return {
        "kind": "feature",
        "title": title,
        "body": f"Do {title}.",
        "acceptance": [f"{title} works"],
        "repo": "owner/name",
        "ref": "main",
        "impacted_paths": paths,
        "size": size,
        "depends_on": depends_on,
    }


def _story(*tickets: dict[str, Any], open_questions: list[str] | None = None) -> dict[str, Any]:
    return {
        "summary": "Cut in order.",
        "tickets": list(tickets),
        "open_questions": open_questions or [],
    }


INDEPENDENT = _story(_ticket("Rates", ["shop/rates.py"], []), _ticket("Docs", ["docs/a.md"], []))
CHAIN = _story(
    _ticket("Rates", ["shop/rates.py"], []),
    _ticket("Quote", ["shop/quote.py"], [0], "M"),
)
SHARED = _story(
    _ticket("Rates", ["shop/rates.py", "shop/a.py"], []),
    _ticket("Rounding", ["shop/rates.py"], []),
)
UNKNOWN = _story(_ticket("Rates", ["shop/rates.py"], []), _ticket("Docs", [], []))


class FakeRunner:
    """Stands in for `runtime.runner.run`: one canned output per agent, a trace row per call."""

    def __init__(
        self, prd: Mapping[str, Any] | None = None, story: Mapping[str, Any] | None = None
    ):
        self.prd = _prd() if prd is None else dict(prd)
        self.story = dict(CHAIN if story is None else story)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, spec: LLMSpec, payload: Mapping[str, Any], trace: Trace, **kwargs: Any
    ) -> BaseModel:
        self.calls.append({"spec": spec, "payload": dict(payload), **kwargs})
        trace.record(
            TraceRow(
                run_id=f"row-{len(self.calls)}",
                pipeline_run_id=kwargs["pipeline_run_id"],
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=dict(payload),
                cost_usd=0.5,
            )
        )
        answer = self.prd if spec.name == ARCHITECT[0] else self.story
        return spec.output_class.model_validate(answer)

    def names(self) -> list[str]:
        return [c["spec"].name for c in self.calls]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    (fixture,) = build(["off-by-one-red"], dest=tmp_path_factory.mktemp("repos"))
    return fixture.path


@pytest.fixture(scope="module")
def agents() -> PlanAgents:
    return resolve_plan_agents(allow_unpublished=True)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


@pytest.fixture
def plans(tmp_path: Path) -> Path:
    return tmp_path / "plans"


def _start(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path, runner: FakeRunner, **more: Any
) -> plan_loop.PlanResult:
    return run_plan(
        agents, repo, trace, idea="Quote shipping", plans_dir=plans, runner=runner, **more
    )


def _again(
    agents: PlanAgents,
    repo: Path,
    trace: Trace,
    plans: Path,
    runner: FakeRunner,
    first: plan_loop.PlanResult,
    **more: Any,
) -> plan_loop.PlanResult:
    return run_plan(
        agents,
        repo,
        trace,
        prd_source=first.prd_path.read_text(encoding="utf-8"),
        plans_dir=plans,
        runner=runner,
        **more,
    )


# --- agents


def test_the_two_agents_resolve_as_llm_agents(agents: PlanAgents) -> None:
    assert agents.architect.name == ARCHITECT[0]
    assert agents.breakdown.name == BREAKDOWN[0]


def test_an_agent_that_is_not_an_llm_agent_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotLLM:
        address = "product-architect@0.1.0"

    monkeypatch.setattr(plan_loop, "resolve", lambda *a, **k: NotLLM())
    with pytest.raises(UsageError, match="not an LLM agent"):
        resolve_plan_agents(allow_unpublished=True)


# --- the first invocation


def test_an_idea_writes_the_prd_stops_and_files_nothing(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    result = _start(agents, repo, trace, plans, runner)
    assert result.status == AWAITING_APPROVAL
    assert result.exit_code == ExitCode.OK
    assert runner.names() == [ARCHITECT[0]]
    assert result.prd_path == plans / f"{result.pipeline_run_id}.md"
    assert "## Problem" in result.prd_path.read_text(encoding="utf-8")
    assert WorkItems(trace).list() == []
    assert runner.calls[0]["pipeline_run_id"] == result.pipeline_run_id
    assert result.cost_usd == 0.5


def test_the_prd_is_pinned_to_the_commit_the_ref_resolved_to(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    result = _start(agents, repo, trace, plans, runner, ref="HEAD", repository="owner/name")
    assert len(result.prd.ref) == 40
    assert runner.calls[0]["payload"]["ref"] == result.prd.ref
    assert result.prd.repository == "owner/name"


def test_a_repository_that_is_missing_is_not_found(
    agents: PlanAgents, trace: Trace, plans: Path, tmp_path: Path
) -> None:
    runner = FakeRunner()
    with pytest.raises(plan_loop.NotFoundError):
        _start(agents, tmp_path / "nowhere", trace, plans, runner)
    assert runner.calls == []


def test_an_idea_and_a_prd_together_are_a_usage_error(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    with pytest.raises(UsageError, match="either"):
        run_plan(agents, repo, trace, idea="x", prd_source="y", plans_dir=plans)
    with pytest.raises(UsageError, match="either"):
        run_plan(agents, repo, trace, plans_dir=plans)


def test_an_answer_with_an_idea_is_a_usage_error(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    with pytest.raises(UsageError, match="no questions"):
        _start(agents, repo, trace, plans, runner, answers=["1=x"])
    assert runner.calls == []


# --- questions and answers


def test_open_questions_keep_the_plan_from_going_on(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS))
    first = _start(agents, repo, trace, plans, runner)
    assert first.status == AWAITING_ANSWERS
    assert first.exit_code == ExitCode.OK
    second = _again(agents, repo, trace, plans, runner, first, approve=True)
    assert second.status == AWAITING_ANSWERS
    assert second.exit_code == ExitCode.ERROR
    assert runner.names() == [ARCHITECT[0]]
    assert WorkItems(trace).list() == []


def test_approving_with_one_question_left_is_refused(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS))
    first = _start(agents, repo, trace, plans, runner)
    second = _again(agents, repo, trace, plans, runner, first, approve=True, answers=["1=euros"])
    assert second.status == AWAITING_ANSWERS
    assert [q.answer for q in second.prd.questions] == ["euros", None]
    assert "A: euros" in second.prd_path.read_text(encoding="utf-8")
    assert BREAKDOWN[0] not in runner.names()


def test_answers_on_the_command_line_let_approval_through(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS))
    first = _start(agents, repo, trace, plans, runner)
    done = _again(
        agents,
        repo,
        trace,
        plans,
        runner,
        first,
        approve=True,
        answers=["which currency does it show?=euros", "2=Yes, a = b is fine"],
    )
    assert done.status == FILED
    assert runner.names() == [ARCHITECT[0], BREAKDOWN[0]]
    sent = runner.calls[1]["payload"]["prd"]
    assert "A: euros" in sent
    assert "A: Yes, a = b is fine" in sent


def test_answers_written_in_the_file_are_read_back(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS))
    first = _start(agents, repo, trace, plans, runner)
    edited = first.prd_path.read_text(encoding="utf-8").replace("  A:", "  A: yes", 2)
    done = run_plan(
        agents, repo, trace, prd_source=edited, approve=True, plans_dir=plans, runner=runner
    )
    assert done.status == FILED


def test_a_multi_line_answer_survives_the_file(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS[:1]))
    first = _start(agents, repo, trace, plans, runner)
    source = first.prd_path.read_text(encoding="utf-8").replace("  A:", "  A: euros\n    and cents")
    second = run_plan(agents, repo, trace, prd_source=source, plans_dir=plans, runner=runner)
    assert second.prd.questions[0].answer == "euros\nand cents"
    assert parse_prd(second.prd_path.read_text(encoding="utf-8")).questions[0].answer == (
        "euros\nand cents"
    )


def test_an_unknown_answer_is_a_usage_error_and_changes_nothing(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS))
    first = _start(agents, repo, trace, plans, runner)
    before = first.prd_path.read_text(encoding="utf-8")
    with pytest.raises(UsageError, match="matches no open question"):
        _again(agents, repo, trace, plans, runner, first, answers=["Why is the sky blue?=x"])
    assert first.prd_path.read_text(encoding="utf-8") == before


def test_a_prd_without_questions_refuses_an_answer(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    first = _start(agents, repo, trace, plans, runner)
    with pytest.raises(UsageError, match="no open question"):
        _again(agents, repo, trace, plans, runner, first, answers=["1=x"])


QS = [Question("Which currency?", ""), Question("Is a = b allowed?", "")]


@pytest.mark.parametrize(
    ("text", "index", "answer"),
    [
        ("Which currency?=euros", 0, "euros"),
        ("  which   CURRENCY? = euros ", 0, "euros"),
        ("1=euros", 0, "euros"),
        ("Q2=no", 1, "no"),
        ("Is a = b allowed?=yes", 1, "yes"),
        ("Which currency?=a=b", 0, "a=b"),
    ],
)
def test_an_answer_names_its_question_by_text_or_number(text: str, index: int, answer: str) -> None:
    assert parse_answer(text, QS) == (index, answer)


@pytest.mark.parametrize("text", ["3=x", "0=x", "nonsense", "=x", "Which currency?"])
def test_an_answer_that_names_no_question_is_refused(text: str) -> None:
    with pytest.raises(UsageError, match="matches no open question"):
        parse_answer(text, QS)


def test_an_empty_answer_is_refused() -> None:
    with pytest.raises(UsageError, match="empty"):
        parse_answer("1=  ", QS)


def test_two_answers_to_one_question_in_one_command_are_refused() -> None:
    prd = Prd("r", "repo", "ref", "idea", "# PRD", [Question("A?", ""), Question("B?", "")])
    with pytest.raises(UsageError, match="twice"):
        apply_answers(prd, ["1=x", "A?=y"])


def test_a_later_command_may_replace_an_earlier_answer() -> None:
    prd = Prd("r", "repo", "ref", "idea", "# PRD", [Question("A?", "", "old")])
    assert apply_answers(prd, ["1=new"]) == [0]
    assert prd.questions[0].answer == "new"


# --- the second invocation: approval and tickets


def test_approving_files_the_tickets_in_todo_under_one_run_id(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(story=CHAIN)
    first = _start(agents, repo, trace, plans, runner)
    done = _again(agents, repo, trace, plans, runner, first, approve=True)
    assert done.status == FILED
    assert done.pipeline_run_id == first.pipeline_run_id
    assert {c["pipeline_run_id"] for c in runner.calls} == {first.pipeline_run_id}
    store = WorkItems(trace)
    first_item, second_item = store.list()
    assert [first_item.status, second_item.status] == ["todo", "todo"]
    assert first_item.parent == second_item.parent == f"prd:{first.pipeline_run_id}"
    assert second_item.depends_on == (first_item.id,)
    assert (first_item.size, second_item.size) == ("S", "M")
    for item in (first_item, second_item):
        (created,) = store.events(item.id)
        assert created.actor == ACTOR
        assert created.pipeline_run_id == first.pipeline_run_id
        assert item.pipeline_run_ids == ()
    assert store.ready().promoted == (first_item.id,)
    assert [item.status for item in store.list()] == ["ready", "todo"]


def test_the_plan_loop_never_makes_a_ticket_ready(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(story=INDEPENDENT)
    first = _start(agents, repo, trace, plans, runner)
    _again(agents, repo, trace, plans, runner, first, approve=True)
    assert {item.status for item in WorkItems(trace).list()} == {"todo"}


def test_the_approved_prd_file_says_it_is_filed(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(story=CHAIN)
    first = _start(agents, repo, trace, plans, runner)
    done = _again(agents, repo, trace, plans, runner, first, approve=True)
    stored = parse_prd(done.prd_path.read_text(encoding="utf-8"))
    assert stored.status == FILED
    assert stored.tickets == [item.id for item in done.items]


def test_the_breakdown_gets_the_prd_the_repository_and_the_pinned_ref(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    first = _start(agents, repo, trace, plans, runner, repository="owner/name")
    _again(agents, repo, trace, plans, runner, first, approve=True)
    payload = runner.calls[1]["payload"]
    assert payload["repository"] == "owner/name"
    assert payload["ref"] == first.prd.ref
    assert "## Problem" in payload["prd"]
    assert "plan_run_id" not in payload["prd"]


def test_a_filed_prd_cannot_be_filed_twice(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    first = _start(agents, repo, trace, plans, runner)
    done = _again(agents, repo, trace, plans, runner, first, approve=True)
    calls = len(runner.calls)
    with pytest.raises(PlanError, match="already filed"):
        _again(agents, repo, trace, plans, runner, done, approve=True)
    assert len(runner.calls) == calls
    assert len(WorkItems(trace).list()) == 2


def test_a_prd_filed_in_the_store_is_refused_even_if_its_file_says_draft(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    first = _start(agents, repo, trace, plans, runner)
    draft = first.prd_path.read_text(encoding="utf-8")
    _again(agents, repo, trace, plans, runner, first, approve=True)
    with pytest.raises(PlanError, match="already filed"):
        run_plan(
            agents, repo, trace, prd_source=draft, approve=True, plans_dir=plans, runner=runner
        )


def test_an_idea_and_approve_together_ask_for_the_prd_first(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner()
    result = _start(agents, repo, trace, plans, runner, approve=True)
    assert result.status == FILED
    assert runner.names() == [ARCHITECT[0], BREAKDOWN[0]]


def test_an_idea_and_approve_with_open_questions_stops_at_the_questions(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS))
    result = _start(agents, repo, trace, plans, runner, approve=True)
    assert result.status == AWAITING_ANSWERS
    assert result.exit_code == ExitCode.ERROR
    assert runner.names() == [ARCHITECT[0]]


@pytest.mark.parametrize(
    "source", ["", "no header", "---\nplan_run_id: 1\n---\n", "---\nbad\n---\n"]
)
def test_a_file_that_is_not_a_plan_is_a_plan_error(source: str) -> None:
    with pytest.raises(PlanError):
        parse_prd(source)


def test_a_question_line_that_cannot_be_read_is_a_plan_error() -> None:
    prd = Prd("r", "repo", "ref", "idea", "# PRD", [Question("A?", "")])
    broken = prd.render().replace("  A:", "  X:")
    with pytest.raises(PlanError, match="not `  Why:` or `  A:`"):
        parse_prd(broken)


def test_the_file_round_trips() -> None:
    prd = Prd(
        "run-1",
        'owner/"name"',
        "a" * 40,
        "line one\nline two",
        prd_text(plan_loop_output()),
        [Question("A?", "Because", "yes"), Question("B?", "Why not", None)],
        tickets=["wi-1"],
    )
    again = parse_prd(prd.render())
    assert again == prd


def plan_loop_output() -> BaseModel:
    return resolve_plan_agents(allow_unpublished=True).architect.output_class.model_validate(_prd())


# --- conflicts


def test_two_tickets_on_one_file_are_made_sequential(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(story=SHARED)
    first = _start(agents, repo, trace, plans, runner)
    done = _again(agents, repo, trace, plans, runner, first, approve=True)
    one, two = done.items
    assert two.depends_on == (one.id,)
    (conflict,) = done.conflicts
    assert conflict.resolution == "serialized"
    assert conflict.paths == ("shop/rates.py",)
    assert "runs after" in done.text()


def test_a_conflict_the_dependencies_already_cover_adds_nothing() -> None:
    story = _story(
        _ticket("A", ["shop/x.py"], []),
        _ticket("B", ["shop/x.py"], [0]),
    )
    output = resolve_plan_agents(allow_unpublished=True).breakdown.output_class.model_validate(
        story
    )
    batch, conflicts = plan_batch(output)
    assert conflicts == []
    assert batch[1].depends_on == (0,)


def test_a_ticket_with_no_paths_is_reported_not_serialized(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(story=UNKNOWN)
    first = _start(agents, repo, trace, plans, runner)
    done = _again(agents, repo, trace, plans, runner, first, approve=True)
    assert [item.depends_on for item in done.items] == [(), ()]
    assert [c.resolution for c in done.conflicts] == ["unknown_paths"]
    assert "no impacted paths" in done.text()


def test_serializing_a_chain_of_conflicts_never_makes_a_cycle() -> None:
    story = _story(
        _ticket("A", ["x.py"], []),
        _ticket("B", ["x.py"], []),
        _ticket("C", ["x.py"], []),
    )
    output = resolve_plan_agents(allow_unpublished=True).breakdown.output_class.model_validate(
        story
    )
    batch, _ = plan_batch(output)
    assert [item.depends_on for item in batch] == [(), (0,), (1,)]


# --- the breakdown's own questions


def test_a_breakdown_question_does_not_block_the_plan_but_is_reported(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(story=_story(_ticket("Rates", ["a.py"], []), open_questions=["Which db?"]))
    first = _start(agents, repo, trace, plans, runner)
    done = _again(agents, repo, trace, plans, runner, first, approve=True)
    assert done.status == FILED
    assert done.breakdown_questions == ("Which db?",)
    assert "Which db?" in done.prd_path.read_text(encoding="utf-8")
    assert done.to_json()["breakdown_open_questions"] == ["Which db?"]
    assert {item.status for item in done.items} == {"todo"}


# --- all or nothing


def test_a_failed_store_write_files_no_ticket_and_leaves_the_prd_a_draft(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    first = _start(agents, repo, trace, plans, runner)
    draft = first.prd_path.read_text(encoding="utf-8")

    def broken(self: WorkItems, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(WorkItems, "add_many", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        _again(agents, repo, trace, plans, runner, first, approve=True)
    assert WorkItems(trace).list() == []
    assert first.prd_path.read_text(encoding="utf-8") == draft


def test_a_failed_breakdown_leaves_the_stored_prd_and_files_nothing(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    class Failing(FakeRunner):
        def __call__(self, spec: LLMSpec, *args: Any, **kwargs: Any) -> BaseModel:
            if spec.name == BREAKDOWN[0]:
                raise RuntimeError("model down")
            return super().__call__(spec, *args, **kwargs)

    runner = Failing()
    first = _start(agents, repo, trace, plans, runner)
    with pytest.raises(RuntimeError, match="model down"):
        _again(agents, repo, trace, plans, runner, first, approve=True)
    assert WorkItems(trace).list() == []
    assert parse_prd(first.prd_path.read_text(encoding="utf-8")).status == "draft"


# --- the report


def test_the_json_document_names_the_tickets_and_the_next_step(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(_prd(QUESTIONS))
    first = _start(agents, repo, trace, plans, runner)
    document = first.to_json()
    assert document["status"] == AWAITING_ANSWERS
    assert document["approved"] is False
    assert document["unanswered"] == 2
    assert document["tickets"] == []
    assert "--answer" in first.text()
    assert f"--prd @{first.prd_path}" in first.text()


def test_the_text_of_a_filed_plan_lists_each_ticket(
    agents: PlanAgents, repo: Path, trace: Trace, plans: Path
) -> None:
    runner = FakeRunner(story=CHAIN)
    first = _start(agents, repo, trace, plans, runner)
    done = _again(agents, repo, trace, plans, runner, first, approve=True)
    assert "Filed 2 ticket(s) in todo" in done.text()
    assert "wi-2 [M] Quote (after wi-1)" in done.text()
    assert "factory ticket ready" in done.text()
    assert done.to_json()["tickets"][1]["depends_on"] == ["wi-1"]


# --- factory plan


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(plan_loop, "run", runner)
    return runner


def _plan_argv(repo: Path, plans: Path, *extra: str) -> list[str]:
    return ["plan", str(repo), "--plans-dir", str(plans), "--allow-unpublished", *extra]


def test_factory_plan_prints_one_json_document_for_an_idea(
    cli: FakeRunner, repo: Path, plans: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_plan_argv(repo, plans, "--idea", "Quote shipping", "--json")) == ExitCode.OK
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["status"] == AWAITING_APPROVAL
    assert Path(document["prd_path"]).is_file()
    assert "## Problem" in captured.err


def test_factory_plan_takes_an_idea_from_a_file(
    cli: FakeRunner, repo: Path, plans: Path, tmp_path: Path
) -> None:
    idea = tmp_path / "idea.md"
    idea.write_text("Quote shipping by weight", encoding="utf-8")
    assert main(_plan_argv(repo, plans, "--idea", f"@{idea}")) == ExitCode.OK
    assert cli.calls[0]["payload"]["idea"] == "Quote shipping by weight"


def test_factory_plan_runs_both_steps(
    cli: FakeRunner, repo: Path, plans: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.prd = _prd(QUESTIONS)
    assert main(_plan_argv(repo, plans, "--idea", "Quote shipping", "--json")) == ExitCode.OK
    first = json.loads(capsys.readouterr().out)
    argv = _plan_argv(repo, plans, "--prd", f"@{first['prd_path']}", "--approve", "--json")
    assert main([*argv, "--answer", "1=euros"]) == ExitCode.ERROR
    capsys.readouterr()
    assert main([*argv, "--answer", "1=euros", "--answer", "2=yes"]) == ExitCode.OK
    document = json.loads(capsys.readouterr().out)
    assert document["pipeline_run_id"] == first["pipeline_run_id"]
    assert document["status"] == FILED
    assert [t["status"] for t in document["tickets"]] == ["todo", "todo"]
    assert main(argv) == ExitCode.ERROR
    assert "already filed" in capsys.readouterr().err


def test_factory_plan_with_unanswered_questions_and_approve_exits_1_with_the_document(
    cli: FakeRunner, repo: Path, plans: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.prd = _prd(QUESTIONS)
    assert main(_plan_argv(repo, plans, "--idea", "x", "--approve", "--json")) == ExitCode.ERROR
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == AWAITING_ANSWERS
    assert document["unanswered"] == 2


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--idea", "x", "--prd", "@f"],
        ["--idea", "x", "--answer", "1=y"],
        ["--idea", "  "],
        ["--prd", "plan.md"],
        ["--prd", "@f", "--ref", "main"],
        ["--prd", "@f", "--repository", "o/n"],
    ],
)
def test_factory_plan_refuses_a_bad_combination_before_calling_a_model(
    cli: FakeRunner, repo: Path, plans: Path, extra: list[str]
) -> None:
    assert main(_plan_argv(repo, plans, *extra)) == ExitCode.USAGE
    assert cli.calls == []


def test_factory_plan_exits_3_for_a_missing_prd_file(
    cli: FakeRunner, repo: Path, plans: Path, tmp_path: Path
) -> None:
    argv = _plan_argv(repo, plans, "--prd", f"@{tmp_path / 'none.md'}")
    assert main(argv) == ExitCode.NOT_FOUND


def test_factory_plan_exits_3_for_a_missing_repository(
    cli: FakeRunner, plans: Path, tmp_path: Path
) -> None:
    assert main(_plan_argv(tmp_path / "none", plans, "--idea", "x")) == ExitCode.NOT_FOUND


def test_factory_plan_needs_the_api_key_to_call_a_model(
    cli: FakeRunner, repo: Path, plans: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert main(_plan_argv(repo, plans, "--idea", "x")) == ExitCode.AUTH_OR_CONFIG
    assert cli.calls == []


def test_factory_plan_answers_need_no_key_until_approval(
    cli: FakeRunner, repo: Path, plans: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli.prd = _prd(QUESTIONS)
    assert main(_plan_argv(repo, plans, "--idea", "x")) == ExitCode.OK
    (stored,) = plans.glob("*.md")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert (
        main(_plan_argv(repo, plans, "--prd", f"@{stored}", "--answer", "1=euros")) == ExitCode.OK
    )
    assert main(_plan_argv(repo, plans, "--prd", f"@{stored}", "--approve")) == (
        ExitCode.AUTH_OR_CONFIG
    )
