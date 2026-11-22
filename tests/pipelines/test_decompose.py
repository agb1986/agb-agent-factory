"""Tests for `pipelines.decompose`: a blueprint to a `refactor` ticket, with no model and no key.

The blueprints come from the ground truth of `legacy-billing`; the store is a DuckDB file under
`tmp_path`.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.graders.ground_truth import load_truth
from factory.exit import NotFoundError, UsageError
from pipelines import decompose
from pipelines.decompose import (
    ACTOR,
    ARCHAEOLOGY,
    EXTRACTION,
    JSON_HEADING,
    TITLE_CHARS,
    blueprint_of,
    extraction_ticket,
    file_extraction_ticket,
    render_blueprint,
    resolve_decompose_agents,
    ticket_body,
)
from registry.decomposition_schemas import ExtractionBlueprint
from runtime.trace import Trace
from runtime.work_items import WorkItems
from tests.evals.test_ground_truth_grader import truth_blueprint

REPO = "acme/legacy-billing"
REF = "main"
RUN = "decompose-1"
FEATURES = {feature.id: feature for feature in load_truth("legacy-billing").features}


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
