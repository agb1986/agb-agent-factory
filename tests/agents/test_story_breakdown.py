"""Offline tests of the story-breakdown agent: manifest, models, prompt, eval cases."""

from typing import Any, get_args

import anthropic
import pytest
from pydantic import ValidationError

from evals.cases import Case, load_cases
from fixtures.build import discover
from registry.common_schemas import Ticket
from registry.resolver import AGENTS_DIR, resolve
from registry.spec import LLMSpec
from runtime.runner import build_request
from runtime.work_items import SIZES
from tools import TOOLS, tool_defs

AGENT_DIR = AGENTS_DIR / "story-breakdown"


@pytest.fixture(scope="module")
def spec() -> LLMSpec:
    found = resolve("story-breakdown", "^0.1")
    assert isinstance(found, LLMSpec)
    return found


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    return load_cases(AGENT_DIR / "evals" / "cases.yaml")


def _ticket(**overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "kind": "feature",
        "title": "Add a wishlist",
        "body": "Add `storefront/wishlist.py` and wire it into the API.",
        "acceptance": ["Adding an unknown product raises KeyError."],
        "repo": "acme/storefront",
        "ref": "head",
        "impacted_paths": ["storefront/api.py"],
        "size": "M",
        "depends_on": [],
    }  # fmt: skip
    return ticket | overrides


def _output(*tickets: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "summary": "One ticket.",
        "tickets": list(tickets) or [_ticket()],
        "open_questions": [],
    }  # fmt: skip
    return output | overrides


def _parse(spec: LLMSpec, *tickets: dict[str, Any], **overrides: Any) -> Any:
    return spec.output_class.model_validate(_output(*tickets, **overrides))


def _planned(spec: LLMSpec) -> Any:
    """The ticket model inside the output model."""
    return get_args(spec.output_class.model_fields["tickets"].annotation)[0]


# --- manifest


def test_manifest_resolves_to_the_documented_version(spec: LLMSpec) -> None:
    assert spec.address == "story-breakdown@0.1.0"
    assert spec.model == "claude-sonnet-5-5"
    assert spec.effort == "high"
    assert spec.sandbox is False
    assert spec.evals.min_pass_rate == 0.75
    assert spec.limits.max_tool_turns == 15
    assert spec.limits.max_tokens_total is not None


def test_tools_are_the_four_read_only_ones(spec: LLMSpec) -> None:
    assert set(spec.tools.allow) == {"git.read_file", "rg.search", "deps.graph", "deps.impact"}
    assert set(spec.tools.allow) <= set(TOOLS)
    assert len(tool_defs(spec.tools.allow, spec.tools.deny)) == len(spec.tools.allow)
    # Read-only tools only: the agent files nothing, the plan loop does.
    assert not any(name.startswith(("work", "ticket", "github")) for name in spec.tools.allow)


def test_models_are_the_ones_the_manifest_names(spec: LLMSpec) -> None:
    assert spec.input_class.__name__ == "StoryInput"
    assert spec.output_class.__name__ == "StoryOutput"


def test_request_has_no_sampling_params_and_uses_structured_output(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "{}"}])
    for key in ("temperature", "top_p", "top_k", "tool_choice"):
        assert key not in request
    assert request["model"] == "claude-sonnet-5-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_output_schema_passes_the_sdk_transform(spec: LLMSpec) -> None:
    schema = anthropic.transform_schema(spec.output_class)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"summary", "tickets", "open_questions"}


# --- input


def test_input_needs_a_prd_a_repository_and_a_ref(spec: LLMSpec) -> None:
    good = {"repo_path": "/repo", "prd": "x", "repository": "acme/shop", "ref": "head"}
    spec.input_class.model_validate(good)
    spec.input_class.model_validate(good | {"repository": "/work/shop"})
    for bad in (
        {key: value for key, value in good.items() if key != "prd"},
        good | {"prd": ""},
        good | {"ref": ""},
        good | {"repository": ""},
        good | {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            spec.input_class.model_validate(bad)


# --- output


def test_a_ticket_is_the_ticket_shape_plus_a_size_and_dependencies(spec: LLMSpec) -> None:
    from_ticket = set(Ticket.model_fields)
    planned = set(_planned(spec).model_fields)
    assert planned == from_ticket | {"size", "depends_on"}


def test_the_sizes_are_the_ones_the_work_item_store_accepts(spec: LLMSpec) -> None:
    planned = _planned(spec)
    assert get_args(planned.model_fields["size"].annotation) == SIZES


def test_output_round_trips_through_json(spec: LLMSpec) -> None:
    output = _parse(
        spec,
    )
    assert spec.output_class.model_validate_json(output.model_dump_json()) == output


def test_a_planned_ticket_converts_to_a_plain_ticket(spec: LLMSpec) -> None:
    output = _parse(
        spec,
    )
    ticket = output.tickets[0].as_ticket()
    assert type(ticket) is Ticket
    assert ticket.title == "Add a wishlist"
    assert "size" not in ticket.model_dump()


def test_a_dependency_chain_in_build_order_is_valid(spec: LLMSpec) -> None:
    output = _parse(
        spec,
        _ticket(title="Money first", impacted_paths=["storefront/money.py"]),
        _ticket(title="Then the cart", impacted_paths=["storefront/cart.py"], depends_on=[0]),
        _ticket(title="Then the API", impacted_paths=["storefront/api.py"], depends_on=[0, 1]),
    )
    assert output.ancestors(2) == {0, 1}
    assert output.ancestors(0) == set()


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"tickets": []}, "at least 1"),
        ({"summary": ""}, "at least 1"),
        ({"open_questions": "none"}, "list"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_a_malformed_output_is_rejected(spec: LLMSpec, bad: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        spec.output_class.model_validate(_output(**bad))


@pytest.mark.parametrize(
    "bad",
    [
        {"acceptance": []},
        {"acceptance": [" "]},
        {"size": "XL"},
        {"depends_on": "0"},
        {"title": ""},
        {"title": "two\nlines"},
        {"repo": "not a repo"},
        {"impacted_paths": ["/etc/passwd"]},
        {"impacted_paths": ["../outside.py"]},
        {"impacted_paths": ["storefront\\api.py"]},
        {"impacted_paths": ["storefront/api.py", "storefront/api.py"]},
        {"id": "wi-1"},
    ],
)
def test_a_malformed_ticket_is_rejected(spec: LLMSpec, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec.output_class.model_validate(_output(_ticket(**bad)))


@pytest.mark.parametrize(
    ("tickets", "message"),
    [
        ([_ticket(depends_on=[1])], "not a ticket"),
        ([_ticket(depends_on=[-1])], "not a ticket"),
        ([_ticket(depends_on=[0])], "itself"),
        ([_ticket(), _ticket(depends_on=[0, 0])], "twice"),
        ([_ticket(depends_on=[1]), _ticket()], "build order"),
        ([_ticket(depends_on=[1]), _ticket(depends_on=[0])], "cycle: 0 -> 1 -> 0"),
        (
            [_ticket(depends_on=[2]), _ticket(depends_on=[0]), _ticket(depends_on=[1])],
            "cycle: 0 -> 2 -> 1 -> 0",
        ),
    ],
)
def test_a_bad_dependency_graph_is_rejected(
    spec: LLMSpec, tickets: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        spec.output_class.model_validate(_output(*tickets))


def test_unordered_tickets_that_share_a_path_are_reported(spec: LLMSpec) -> None:
    output = _parse(
        spec,
        _ticket(title="Wishlist", impacted_paths=["storefront/api.py", "storefront/cart.py"]),
        _ticket(title="Ratings", impacted_paths=["storefront/api.py", "storefront/catalog.py"]),
        _ticket(title="Docs", impacted_paths=["README.md"]),
    )
    assert output.parallel_conflicts() == [(0, 1, ["storefront/api.py"])]


def test_a_dependency_orders_tickets_even_when_they_share_a_path(spec: LLMSpec) -> None:
    output = _parse(
        spec,
        _ticket(title="Wishlist"),
        _ticket(title="Ratings", depends_on=[0]),
        _ticket(title="Docs", impacted_paths=["README.md"], depends_on=[1]),
    )
    assert output.parallel_conflicts() == []


def test_a_transitive_dependency_orders_tickets(spec: LLMSpec) -> None:
    output = _parse(
        spec,
        _ticket(title="A"),
        _ticket(title="B", impacted_paths=["storefront/cart.py"], depends_on=[0]),
        _ticket(title="C", depends_on=[1]),
    )
    assert output.parallel_conflicts() == []


def test_unordered_tickets_with_no_paths_are_reported(spec: LLMSpec) -> None:
    output = _parse(spec, _ticket(impacted_paths=[]), _ticket(impacted_paths=["storefront/api.py"]))
    assert output.parallel_conflicts() == [(0, 1, [])]


# --- system prompt


def test_prompt_is_long_enough_to_be_cached(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    assert len(text) / 4 > 500


def test_prompt_names_every_tool_every_field_and_the_one_rule(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8")
    for tool in spec.tools.allow:
        assert f"`{tool}`" in text
    for field in spec.output_class.model_fields:
        assert f"`{field}`" in text
    for field in spec.input_class.model_fields:
        assert f"`{field}`" in text
    planned = _planned(spec)
    for field in planned.model_fields:
        assert f"`{field}`" in text
    assert "disjoint `impacted_paths`" in text
    assert "adding the edge to `depends_on`" in text


def test_prompt_holds_no_gate_thresholds(spec: LLMSpec) -> None:
    text = spec.system_prompt_file.read_text(encoding="utf-8").lower()
    assert "min_pass_rate" not in text
    assert "threshold" not in text


# --- eval cases


def test_there_are_four_cases_with_unique_ids(cases: list[Case]) -> None:
    ids = [case.id for case in cases]
    assert len(ids) >= 4
    assert len(set(ids)) == len(ids)


def test_cases_reference_existing_fixtures_and_complete_into_inputs(
    spec: LLMSpec, cases: list[Case]
) -> None:
    fixtures = discover()
    for case in cases:
        assert case.fixture in fixtures
        spec.input_class.model_validate({"repo_path": "/repo", **case.payload})
        assert case.grader == "rubric"
        assert case.expected.verdict is None
        assert case.expected.rubric is not None


def test_every_case_checks_that_impacted_paths_exist_and_wants_tickets(
    cases: list[Case],
) -> None:
    for case in cases:
        rubric = case.expected.rubric
        assert rubric is not None
        assert "tickets.*.impacted_paths" in rubric.paths_exist
        assert "tickets" in rubric.required_fields
        assert any(criterion.required for criterion in rubric.criteria)


def test_the_cases_cover_two_fixtures_and_a_case_for_disjoint_paths(cases: list[Case]) -> None:
    assert len({case.fixture for case in cases}) >= 2
    disjoint = [
        case
        for case in cases
        if case.expected.rubric
        and any("share" in criterion.text for criterion in case.expected.rubric.criteria)
    ]
    assert len(disjoint) >= 2


def test_the_prds_are_written_by_hand_not_empty(cases: list[Case]) -> None:
    for case in cases:
        assert len(case.payload["prd"]) > 150
