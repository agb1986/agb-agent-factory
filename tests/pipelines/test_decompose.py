"""Tests for `pipelines.decompose`: a repository to a blueprint to a `refactor` ticket.

No model is called and no key is needed: the agents are a fake runner's, whose answers come from the
ground truth of `legacy-billing`, and they read the built fixture. The store is a DuckDB file under
`tmp_path`.
"""

import json
import shutil
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from evals.graders.ground_truth import load_truth
from factory.cli import main
from factory.exit import ExitCode, FactoryError, NotFoundError, UsageError
from fixtures.build import BuiltFixture, build
from pipelines import decompose
from pipelines.decompose import (
    ACTOR,
    ARCHAEOLOGY,
    EXTRACTION,
    JSON_HEADING,
    TITLE_CHARS,
    DecomposeAgents,
    DecomposeResult,
    blueprint_of,
    extraction_ticket,
    file_extraction_ticket,
    render_blueprint,
    resolve_decompose_agents,
    run_decompose,
    ticket_body,
)
from registry.decomposition_schemas import ExtractionBlueprint
from registry.spec import LLMSpec
from runtime.budget import Budget, BudgetExceeded
from runtime.errors import AgentError
from runtime.trace import Trace, TraceRow
from runtime.work_items import WorkItems
from tests.evals.test_ground_truth_grader import truth_answer, truth_blueprint
from tools._common import git

REPO = "acme/legacy-billing"
REF = "main"
RUN = "decompose-1"
TRUTH = load_truth("legacy-billing")
FEATURES = {feature.id: feature for feature in TRUTH.features}
ENTRY_POINT = "legacy_billing/cli.py::main"
NAME = "late fees"
START = "legacy_billing/billing.py::apply_late_fees"


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "traces.duckdb") as opened:
        yield opened


def blueprint(**changes: Any) -> ExtractionBlueprint:
    """Return the blueprint of the late fees with `changes` made to it, validated."""
    return ExtractionBlueprint.model_validate(
        truth_blueprint(FEATURES["feature-late-fees"]) | changes
    )


def small(**changes: Any) -> ExtractionBlueprint:
    """Return the smallest blueprint with `changes` made to it, validated."""
    return ExtractionBlueprint.model_validate(
        {
            "feature": "fees",
            "start": "app/fees.py::charge",
            "summary": "Charges fees.",
            "inputs": [],
            "outputs": [],
            "invariants": ["A fee is never negative."],
            "symbols": [{"symbol": "app/fees.py::charge", "role": "Charges."}],
            "uses": [],
            "called_from": [],
            "state": [],
            "pinning_tests": [],
            "open_questions": [],
        }
        | changes
    )


class TestRenderBlueprint:
    @pytest.mark.parametrize("feature", FEATURES)
    def test_everything_in_the_blueprint_is_in_the_markdown(self, feature: str) -> None:
        given = ExtractionBlueprint.model_validate(truth_blueprint(FEATURES[feature]))
        text = " ".join(render_blueprint(given).split())

        def strings(value: object) -> Iterator[str]:
            if isinstance(value, str):
                yield " ".join(value.replace("_", " ").split())
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)

        for string in strings(given.model_dump(mode="json")):
            assert string in text.replace("_", " ")

    def test_the_layout(self) -> None:
        given = small(
            invariants=["One.", "Two."],
            uses=[
                {"symbol": "app/db.py::get", "used_by": "app/fees.py::charge", "need": "The rate."}
            ],
            called_from=[
                {
                    "caller": "app/run.py::main",
                    "callee": "app/fees.py::charge",
                    "expects": "A fee.",
                }
            ],
            state=[
                {
                    "symbol": "app/fees.py::charge",
                    "state": "the fee table",
                    "access": "reads_and_writes",
                    "cut": "Pass it in",
                }
            ],
            pinning_tests=[{"path": "tests/test_fees.py", "pins": "The rate."}],
        )
        assert render_blueprint(given).splitlines() == [
            "Extract the feature fees, which starts at `app/fees.py::charge`.",
            "",
            "Summary: Charges fees.",
            "",
            "## Inputs",
            "",
            "None.",
            "",
            "## Outputs",
            "",
            "None.",
            "",
            "## Invariants",
            "",
            "- One.",
            "- Two.",
            "",
            "## Symbols that move",
            "",
            "- `app/fees.py::charge`: Charges.",
            "",
            "## Outside symbols the feature uses",
            "",
            "- `app/db.py::get`, used by `app/fees.py::charge`: The rate.",
            "",
            "## Outside code that calls the feature",
            "",
            "- `app/run.py::main` calls `app/fees.py::charge`: A fee.",
            "",
            "## Shared state",
            "",
            "- `app/fees.py::charge` reads and writes the fee table. Cut: Pass it in",
            "",
            "## Tests that pin the behaviour",
            "",
            "- `tests/test_fees.py`: The rate.",
            "",
            "## Open questions",
            "",
            "None.",
        ]

    def test_prose_cannot_open_a_fence_or_a_heading(self) -> None:
        given = small(
            summary="# Title\n```json\n{}\n```\n~~~",
            invariants=["One\n## Two\n````\nthree"],
        )
        lines = render_blueprint(given).splitlines()
        assert "Summary: # Title `json {} ` ~" in lines
        assert "- One ## Two ` three" in lines
        assert not [line for line in lines if line.startswith(("```", "~~~", "# ", "## Two"))]


class TestTicketBody:
    @pytest.mark.parametrize("feature", FEATURES)
    def test_the_body_round_trips_to_the_same_blueprint(self, feature: str) -> None:
        given = ExtractionBlueprint.model_validate(truth_blueprint(FEATURES[feature]))
        assert blueprint_of(ticket_body(given)) == given

    def test_the_body_is_the_markdown_and_then_the_json(self) -> None:
        given = blueprint()
        body = ticket_body(given)
        assert body.startswith(render_blueprint(given))
        assert body.count(JSON_HEADING) == 1
        assert body.endswith("\n}\n```\n")

    def test_prose_that_looks_like_the_json_part_round_trips(self) -> None:
        fake = f"x\n{JSON_HEADING}\n\n```json\n{{}}\n```\né "
        given = small(
            summary=fake, feature=f"a {JSON_HEADING}", invariants=[fake], open_questions=[fake]
        )
        assert blueprint_of(ticket_body(given)) == given

    def test_text_added_below_the_json_is_ignored(self) -> None:
        given = blueprint()
        added = f"\n## Review findings to address\n\n{JSON_HEADING}\n\n```json\n{{}}\n```\n"
        assert blueprint_of(ticket_body(given) + added) == given

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("", "no JSON block"),
            ("Written by hand.", "no JSON block"),
            (f"x\n{JSON_HEADING}\n\n```json\n{{}}", "no JSON block"),
            (f"x\n{JSON_HEADING}\n\n```json\n{{}}\n```\n", "not a blueprint"),
            (f"x\n{JSON_HEADING}\n\n```json\nnot json\n```\n", "not a blueprint"),
        ],
    )
    def test_another_body_is_refused(self, body: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            blueprint_of(body)


class TestExtractionTicket:
    @pytest.mark.parametrize("feature", FEATURES)
    def test_the_ticket_of_a_feature(self, feature: str) -> None:
        truth = FEATURES[feature]
        given = ExtractionBlueprint.model_validate(truth_blueprint(truth))
        ticket = extraction_ticket(given, repo=REPO, ref=REF)
        assert ticket.kind == "refactor"
        assert ticket.title == f"Extract {given.feature}"
        assert ticket.body == ticket_body(given)
        assert (ticket.repo, ticket.ref) == (REPO, REF)
        assert len(ticket.acceptance) == len(given.invariants) + len(truth.tests)
        for path in truth.tests:
            assert any(line.startswith(f"{path} passes unchanged: ") for line in ticket.acceptance)
        assert set(ticket.impacted_paths) == {symbol.partition("::")[0] for symbol in truth.symbols}
        assert len(set(ticket.impacted_paths)) == len(ticket.impacted_paths)

    def test_the_acceptance_is_the_invariants_and_then_the_tests(self) -> None:
        given = small(
            invariants=["One\n  holds.", "Two.", "Two."],
            pinning_tests=[
                {"path": "tests/a.py", "pins": "the rate"},
                {"path": "tests/b.py", "pins": "the cap"},
                {"path": "tests/a.py", "pins": "the\nday"},
            ],
        )
        assert extraction_ticket(given, repo=REPO, ref=REF).acceptance == [
            "One holds.",
            "Two.",
            "tests/a.py passes unchanged: the rate; the day",
            "tests/b.py passes unchanged: the cap",
        ]

    @pytest.mark.parametrize("length", [112, 113, 120])
    def test_a_long_feature_name_is_cut_in_the_title_and_whole_in_the_body(
        self, length: int
    ) -> None:
        given = small(feature="f" * length)
        ticket = extraction_ticket(given, repo=REPO, ref=REF)
        assert len(ticket.title) == TITLE_CHARS
        assert ticket.title.endswith("…") is (length > 112)
        assert blueprint_of(ticket.body).feature == "f" * length

    def test_a_feature_name_of_several_lines_makes_a_one_line_title(self) -> None:
        ticket = extraction_ticket(small(feature="late\n fees of\x0bJuly"), repo=REPO, ref=REF)
        assert ticket.title == "Extract late fees of July"

    def test_the_largest_blueprint_makes_a_ticket(self) -> None:
        symbols = [f"app/{'m' * 100}{i:02d}.py::{'c' * 40}" for i in range(24)]
        line, prose = "x" * 120, "y" * 299
        given = small(
            feature=line,
            start=symbols[0],
            summary="z" * 800,
            inputs=[prose] * 10,
            outputs=[prose] * 10,
            invariants=[f"{i:02d}{'y' * 298}" for i in range(12)],
            symbols=[{"symbol": symbol, "role": line} for symbol in symbols],
            uses=[
                {"symbol": f"lib/u{i}.py::f", "used_by": symbols[0], "need": line}
                for i in range(20)
            ],
            called_from=[
                {"caller": f"lib/c{i}.py::f", "callee": symbols[0], "expects": line}
                for i in range(10)
            ],
            state=[
                {"symbol": symbols[i], "state": line, "access": "writes", "cut": line}
                for i in range(10)
            ],
            pinning_tests=[{"path": f"tests/{i}.py", "pins": line} for i in range(8)],
            open_questions=[prose] * 6,
        )
        ticket = extraction_ticket(given, repo=REPO, ref=REF)
        assert len(ticket.acceptance) == 20
        assert len(ticket.impacted_paths) == 24
        assert blueprint_of(ticket.body) == given

    @pytest.mark.parametrize(
        ("repo", "ref", "field"),
        [("not a repo", REF, "repo"), ("", REF, "repo"), (REPO, "", "ref")],
    )
    def test_a_repo_or_ref_no_ticket_takes_is_a_usage_error(
        self, repo: str, ref: str, field: str
    ) -> None:
        with pytest.raises(UsageError, match=rf"does not make a ticket: {field}: "):
            extraction_ticket(small(), repo=repo, ref=ref)


class TestFileExtractionTicket:
    def test_the_item_is_in_the_store(self, trace: Trace) -> None:
        given = blueprint()
        now = datetime(2026, 11, 22, 12, tzinfo=UTC)
        item = file_extraction_ticket(
            trace, given, repo=REPO, ref=REF, pipeline_run_id=RUN, now=lambda: now
        )
        store = WorkItems(trace)
        assert store.get(item.id) == item
        assert [found.id for found in store.list()] == [item.id]
        assert item.status == "todo"
        assert item.ticket().kind == "refactor"
        assert item.ticket() == extraction_ticket(given, repo=REPO, ref=REF)
        assert blueprint_of(item.ticket().body) == given
        assert item.pipeline_run_ids == (RUN,)
        assert item.created_at == now

    def test_the_event_names_the_pipeline_its_run_and_the_feature(self, trace: Trace) -> None:
        item = file_extraction_ticket(
            trace, small(feature="late\nfees"), repo=REPO, ref=REF, pipeline_run_id=RUN
        )
        (event,) = WorkItems(trace).events(item.id)
        assert event.actor == ACTOR == "pipeline:decompose"
        assert event.pipeline_run_id == RUN
        assert event.reason == "filed from the extraction blueprint of late fees"

    def test_nothing_is_stored_when_the_ticket_is_refused(self, trace: Trace) -> None:
        with pytest.raises(UsageError):
            file_extraction_ticket(trace, small(), repo="not a repo", ref=REF, pipeline_run_id=RUN)
        with pytest.raises(UsageError, match="run id"):
            file_extraction_ticket(trace, small(), repo=REPO, ref=REF, pipeline_run_id=" ")
        assert WorkItems(trace).list() == []


class TestResolveDecomposeAgents:
    def test_both_agents_resolve_and_are_read_only(self) -> None:
        agents = resolve_decompose_agents(allow_unpublished=True)
        assert agents.archaeology.name == ARCHAEOLOGY[0]
        assert agents.extraction.name == EXTRACTION[0]
        for spec in (agents.archaeology, agents.extraction):
            assert spec.sandbox_profile is None and spec.files is None

    def test_only_published_versions_unless_allowed(self, tmp_path: Path) -> None:
        with pytest.raises(NotFoundError):
            resolve_decompose_agents(trace_path=tmp_path / "traces.duckdb")

    @pytest.mark.parametrize("name", [ARCHAEOLOGY[0], EXTRACTION[0]])
    @pytest.mark.parametrize("how", ["sandboxed", "files"])
    def test_an_agent_that_may_write_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, name: str, how: str
    ) -> None:
        real = decompose.resolve
        update = {"sandbox": True} if how == "sandboxed" else {"files": object()}

        def writing(asked: str, *args: Any, **kwargs: Any) -> Any:
            spec = real(asked, *args, **kwargs)
            return spec.model_copy(update=update) if asked == name else spec

        monkeypatch.setattr(decompose, "resolve", writing)
        with pytest.raises(UsageError, match=f"{name}.*not read-only"):
            resolve_decompose_agents(allow_unpublished=True)

    @pytest.mark.parametrize("name", [ARCHAEOLOGY[0], EXTRACTION[0]])
    def test_an_agent_that_is_not_an_llm_agent_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        real = decompose.resolve

        def other(asked: str, *args: Any, **kwargs: Any) -> Any:
            if asked == name:
                return SimpleNamespace(address=f"{name}@0.1.0")
            return real(asked, *args, **kwargs)

        monkeypatch.setattr(decompose, "resolve", other)
        with pytest.raises(UsageError, match="is not an LLM agent"):
            resolve_decompose_agents(allow_unpublished=True)


class DecomposeRunner:
    """Stands in for `runtime.runner.run`: the ground truth as both agents would answer it."""

    def __init__(self, *, failing: Mapping[str, Exception] | None = None) -> None:
        self.failing = dict(failing or {})
        self.calls: list[dict[str, Any]] = []

    def __call__(self, spec: LLMSpec, payload: BaseModel, trace: Trace, **kwargs: Any) -> BaseModel:
        clone = kwargs["repo_path"]
        self.calls.append(
            {
                "spec": spec,
                "payload": payload.model_dump(mode="json"),
                "commit": git(["rev-parse", "HEAD"], clone).stdout.decode().strip(),
                "files": sorted(path.name for path in clone.iterdir()),
                **kwargs,
            }
        )
        if spec.name in self.failing:
            raise self.failing[spec.name]
        trace.record(
            TraceRow(
                run_id=kwargs["run_id"],
                pipeline_run_id=kwargs["pipeline_run_id"],
                parent_run_id=kwargs.get("parent_run_id"),
                agent_ref=spec.address,
                kind="llm",
                started_at=datetime.now(UTC),
                duration_ms=1,
                input_json=payload.model_dump(mode="json"),
                cost_usd=0.25,
            )
        )
        if spec.name == ARCHAEOLOGY[0]:
            return spec.output_class.model_validate(truth_answer(TRUTH))
        return spec.output_class.model_validate(blueprint().model_dump(mode="json"))


@pytest.fixture(scope="module")
def agents() -> DecomposeAgents:
    return resolve_decompose_agents(allow_unpublished=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> BuiltFixture:
    (fixture,) = build(["legacy-billing"], dest=tmp_path_factory.mktemp("built"))
    return fixture


@pytest.fixture
def repo(built: BuiltFixture, tmp_path: Path) -> Path:
    """Return a copy of the built fixture that a test may change."""
    return shutil.copytree(built.path, tmp_path / "repos" / "legacy-billing", symlinks=True)


def decompose_repo(
    agents: DecomposeAgents, repo: Path, trace: Trace, runner: DecomposeRunner, **kwargs: Any
) -> DecomposeResult:
    """Run the late fees of `repo` through the pipeline, with any argument replaced by `kwargs`."""
    asked: dict[str, Any] = {"entry_point": ENTRY_POINT, "feature": NAME, "start": START} | kwargs
    return run_decompose(agents, repo, trace=trace, runner=runner, **asked)


class TestRunDecompose:
    def test_the_ticket_is_filed_and_holds_the_blueprint(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        at = datetime(2026, 11, 22, tzinfo=UTC)
        result = decompose_repo(
            agents, repo, trace, DecomposeRunner(), pipeline_run_id=RUN, now=lambda: at
        )
        store = WorkItems(trace)
        (item,) = store.list()
        ticket = item.ticket()
        assert item.id == result.item.id
        assert (item.kind, item.status) == ("refactor", "todo")
        assert item.created_at == at
        assert blueprint_of(ticket.body) == blueprint() == result.blueprint
        assert ticket.repo == str(repo.resolve())
        assert store.cost_usd(item.id) == pytest.approx(0.5)
        assert result.pipeline_run_id == RUN
        assert result.total_cost_usd == pytest.approx(0.5)

    def test_the_second_agent_gets_the_first_ones_output_and_the_feature(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        runner = DecomposeRunner()
        decompose_repo(agents, repo, trace, runner)
        first, second = runner.calls
        assert [first["spec"].name, second["spec"].name] == [ARCHAEOLOGY[0], EXTRACTION[0]]
        assert first["payload"]["entry_point"] == ENTRY_POINT
        assert second["payload"]["archaeology"] == truth_answer(TRUTH)
        assert second["payload"]["feature"] == {"name": NAME, "start": START}
        for call in (first, second):
            assert call["payload"]["repo_path"] == str(call["repo_path"])

    def test_both_calls_share_the_run_id_and_the_budget(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        runner, budget = DecomposeRunner(), Budget(3.0)
        result = decompose_repo(agents, repo, trace, runner, budget=budget)
        first, second = runner.calls
        assert first["pipeline_run_id"] == second["pipeline_run_id"] == result.pipeline_run_id
        assert first["budget"] is second["budget"] is budget
        assert second["parent_run_id"] == first["run_id"] != second["run_id"]

    def test_the_agents_read_a_clone_that_is_gone_afterwards(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        runner = DecomposeRunner()
        decompose_repo(agents, repo, trace, runner)
        first, second = runner.calls
        assert first["repo_path"] == second["repo_path"] != repo.resolve()
        assert not first["repo_path"].exists()

    @pytest.mark.parametrize("name", [ARCHAEOLOGY[0], EXTRACTION[0]])
    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (AgentError("the agent gave up", agent="a@1.0.0"), ExitCode.ERROR),
            (BudgetExceeded(1.0, 1.25), ExitCode.ERROR),
            (UsageError("refused"), ExitCode.USAGE),
        ],
    )
    def test_a_failing_agent_stores_nothing(
        self,
        agents: DecomposeAgents,
        repo: Path,
        trace: Trace,
        name: str,
        error: FactoryError,
        code: ExitCode,
    ) -> None:
        runner = DecomposeRunner(failing={name: error})
        with pytest.raises(FactoryError) as caught:
            decompose_repo(agents, repo, trace, runner)
        assert caught.value is error and caught.value.code == code
        assert WorkItems(trace).list() == []
        assert runner.calls[-1]["spec"].name == name
        assert not runner.calls[-1]["repo_path"].exists()

    def test_the_text_and_the_json_name_the_item_the_feature_the_run_and_the_cost(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        result = decompose_repo(agents, repo, trace, DecomposeRunner(), pipeline_run_id=RUN)
        text, document = result.text(), result.to_json()
        for part in (RUN, NAME, f"filed {result.item.id} (todo)", "cost $0.5000"):
            assert part in text
        assert document["pipeline_run_id"] == RUN
        assert document["feature"] == {"name": NAME, "start": START}
        assert document["ticket"]["id"] == result.item.id
        assert document["total_cost_usd"] == pytest.approx(0.5)
        assert document["extraction"]["output"] == blueprint().model_dump(mode="json")


class TestRefAndWorkTree:
    def test_the_default_ref_is_the_commit_of_head(
        self, agents: DecomposeAgents, repo: Path, built: BuiltFixture, trace: Trace
    ) -> None:
        runner = DecomposeRunner()
        result = decompose_repo(agents, repo, trace, runner)
        head = git(["rev-parse", "HEAD"], repo).stdout.decode().strip()
        assert head in (built.base_sha, built.head_sha)
        assert result.item.ref == head
        assert {call["commit"] for call in runner.calls} == {head}

    def test_a_ref_is_resolved_and_read_whatever_head_is(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        commit = git(["rev-parse", "HEAD"], repo).stdout.decode().strip()
        git(["tag", "before"], repo)
        (repo / "added.py").write_text("", encoding="utf-8")
        git(["add", "added.py"], repo)
        done = git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "later"], repo)
        assert done.returncode == 0
        runner = DecomposeRunner()
        result = decompose_repo(agents, repo, trace, runner, ref="before")
        assert result.item.ref == commit
        assert {call["commit"] for call in runner.calls} == {commit}
        assert all("added.py" not in call["files"] for call in runner.calls)

    @pytest.mark.parametrize("change", ["modified", "staged", "untracked"])
    def test_a_dirty_work_tree_is_refused_without_a_ref(
        self, agents: DecomposeAgents, repo: Path, trace: Trace, change: str
    ) -> None:
        make_dirty(repo, change)
        runner = DecomposeRunner()
        with pytest.raises(UsageError, match="uncommitted changes") as caught:
            decompose_repo(agents, repo, trace, runner)
        assert "--ref HEAD" in str(caught.value.hint)
        assert runner.calls == [] and WorkItems(trace).list() == []

    @pytest.mark.parametrize("change", ["modified", "staged", "untracked"])
    def test_with_a_ref_a_dirty_work_tree_is_left_out(
        self, agents: DecomposeAgents, repo: Path, trace: Trace, change: str
    ) -> None:
        head = git(["rev-parse", "HEAD"], repo).stdout.decode().strip()
        make_dirty(repo, change)
        runner = DecomposeRunner()
        result = decompose_repo(agents, repo, trace, runner, ref="HEAD")
        assert result.item.ref == head
        assert {call["commit"] for call in runner.calls} == {head}
        assert all("added.py" not in call["files"] for call in runner.calls)

    def test_an_ignored_file_is_not_a_change(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "exclude").write_text("ignored.py\n", encoding="utf-8")
        (repo / "ignored.py").write_text("", encoding="utf-8")
        assert decompose_repo(agents, repo, trace, DecomposeRunner()).item.status == "todo"


def make_dirty(repo: Path, change: str) -> None:
    """Leave one uncommitted change of the kind `change` in `repo`."""
    if change == "modified":
        path = repo / "legacy_billing" / "billing.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# later\n", encoding="utf-8")
        return
    (repo / "added.py").write_text("", encoding="utf-8")
    if change == "staged":
        git(["add", "added.py"], repo)


class TestRequest:
    @pytest.mark.parametrize(
        ("change", "error", "says"),
        [
            ({"entry_point": "main"}, UsageError, "entry_point"),
            ({"entry_point": "/etc/x.py"}, UsageError, "entry_point"),
            ({"entry_point": "../x.py::main"}, UsageError, "entry_point"),
            ({"feature": "  "}, UsageError, "name"),
            ({"feature": "x" * 121}, UsageError, "name"),
            ({"start": "apply_late_fees"}, UsageError, "start"),
            ({"start": "a/../x.py::f"}, UsageError, "start"),
            ({"entry_point": "legacy_billing/nope.py::main"}, NotFoundError, "nope.py"),
            ({"start": "legacy_billing/nope.py::f"}, NotFoundError, "nope.py"),
            ({"ref": "no-such-ref"}, NotFoundError, "no-such-ref"),
            ({"ref": "--output=x"}, UsageError, "invalid ref"),
            ({"repository": ""}, UsageError, "repo"),
            ({"repository": "legacy-billing"}, UsageError, "repo"),
        ],
    )
    def test_bad_input_is_refused_before_any_agent_runs(
        self,
        agents: DecomposeAgents,
        repo: Path,
        trace: Trace,
        change: dict[str, str],
        error: type[FactoryError],
        says: str,
    ) -> None:
        runner = DecomposeRunner()
        with pytest.raises(FactoryError, match=says) as caught:
            decompose_repo(agents, repo, trace, runner, **change)
        assert caught.value.code == error.code
        assert runner.calls == [] and WorkItems(trace).list() == []

    def test_a_directory_is_not_a_file_of_the_commit(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        (repo / "pkg.py").mkdir()
        (repo / "pkg.py" / "x").write_text("", encoding="utf-8")
        git(["add", "pkg.py"], repo)
        git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "dir"], repo)
        with pytest.raises(NotFoundError, match="pkg.py"):
            decompose_repo(agents, repo, trace, DecomposeRunner(), entry_point="pkg.py")

    def test_a_path_that_is_not_a_repository_is_refused(
        self, agents: DecomposeAgents, tmp_path: Path, trace: Trace
    ) -> None:
        with pytest.raises(FactoryError) as caught:
            decompose_repo(agents, tmp_path / "nope", trace, DecomposeRunner())
        assert caught.value.code == ExitCode.NOT_FOUND

    def test_an_entry_point_may_be_a_path_alone(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        runner = DecomposeRunner()
        decompose_repo(agents, repo, trace, runner, entry_point="legacy_billing/cli.py")
        assert runner.calls[0]["payload"]["entry_point"] == "legacy_billing/cli.py"

    def test_the_repository_and_the_name_are_taken_as_given(
        self, agents: DecomposeAgents, repo: Path, trace: Trace
    ) -> None:
        runner = DecomposeRunner()
        result = decompose_repo(agents, repo, trace, runner, repository=REPO, feature=f"  {NAME} ")
        assert result.item.repo == REPO
        assert runner.calls[1]["payload"]["feature"]["name"] == NAME


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DecomposeRunner:
    """Point `factory decompose` at a temporary store, a fake key and the fake runner."""
    runner = DecomposeRunner()
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(decompose, "run", runner)
    return runner


def _argv(repo: Path, *extra: str, **changes: str) -> list[str]:
    asked = {"entry-point": ENTRY_POINT, "feature": NAME, "start": START} | {
        name.replace("_", "-"): value for name, value in changes.items()
    }
    flags = [part for name, value in asked.items() for part in (f"--{name}", value)]
    return ["decompose", str(repo), *flags, "--allow-unpublished", *extra]


class TestCommand:
    def test_json_is_one_document_on_stdout_and_the_text_is_on_stderr(
        self,
        cli: DecomposeRunner,
        repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        argv = _argv(repo, "--json", "--repository", REPO, "--ref", "HEAD")
        assert main(argv) == ExitCode.OK
        captured = capsys.readouterr()
        document = json.loads(captured.out)
        assert captured.err.startswith("decompose run ")
        assert (document["ticket"]["kind"], document["ticket"]["status"]) == ("refactor", "todo")
        assert document["ticket"]["repo"] == REPO
        with Trace(tmp_path / "traces.duckdb") as trace:
            (item,) = WorkItems(trace).list()
        assert item.id == document["ticket"]["id"]
        assert blueprint_of(item.ticket().body) == blueprint()

    def test_text_without_json_and_the_budget_flag(
        self, cli: DecomposeRunner, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(_argv(repo, "--budget-usd", "1.5")) == ExitCode.OK
        out = capsys.readouterr().out
        assert out.startswith("decompose run ") and "filed wi-1 (todo)" in out
        assert [call["budget"].usd for call in cli.calls] == [1.5, 1.5]

    def test_it_needs_the_api_key(
        self,
        cli: DecomposeRunner,
        repo: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        assert main(_argv(repo)) == ExitCode.AUTH_OR_CONFIG
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
        assert cli.calls == []

    @pytest.mark.parametrize("key", ["set", "missing"])
    @pytest.mark.parametrize(
        ("extra", "changes", "code"),
        [
            ([], {"entry_point": "main"}, ExitCode.USAGE),
            ([], {"entry_point": "legacy_billing/nope.py"}, ExitCode.NOT_FOUND),
            ([], {"feature": " "}, ExitCode.USAGE),
            ([], {"start": "apply_late_fees"}, ExitCode.USAGE),
            (["--ref", "no-such-ref"], {}, ExitCode.NOT_FOUND),
            (["--repository", "billing"], {}, ExitCode.USAGE),
            (["--budget-usd", "0"], {}, ExitCode.USAGE),
        ],
    )
    def test_bad_input_is_refused_before_the_key_and_the_agents(
        self,
        cli: DecomposeRunner,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        extra: list[str],
        changes: dict[str, str],
        code: ExitCode,
    ) -> None:
        if key == "missing":
            monkeypatch.delenv("ANTHROPIC_API_KEY")
        assert main(_argv(repo, *extra, **changes)) == code
        assert cli.calls == []

    def test_a_repository_that_does_not_exist_and_a_dirty_one_are_refused(
        self, cli: DecomposeRunner, repo: Path, tmp_path: Path
    ) -> None:
        assert main(_argv(tmp_path / "nope")) == ExitCode.NOT_FOUND
        make_dirty(repo, "modified")
        assert main(_argv(repo)) == ExitCode.USAGE
        assert cli.calls == []

    @pytest.mark.parametrize("flag", ["--entry-point", "--feature", "--start"])
    def test_the_three_questions_are_required(
        self, cli: DecomposeRunner, repo: Path, flag: str
    ) -> None:
        argv = _argv(repo)
        at = argv.index(flag)
        assert main(argv[:at] + argv[at + 2 :]) == ExitCode.USAGE
        assert cli.calls == []

    def test_an_unpublished_agent_needs_the_flag(self, cli: DecomposeRunner, repo: Path) -> None:
        argv = [part for part in _argv(repo) if part != "--allow-unpublished"]
        assert main(argv) == ExitCode.NOT_FOUND
        assert cli.calls == []

    def test_a_failing_agent_passes_its_exit_code_on(
        self, cli: DecomposeRunner, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli.failing[EXTRACTION[0]] = BudgetExceeded(1.0, 1.25)
        assert main(_argv(repo, "--json")) == ExitCode.ERROR
        assert capsys.readouterr().out == ""
        with Trace(tmp_path / "traces.duckdb") as trace:
            assert WorkItems(trace).list() == []
