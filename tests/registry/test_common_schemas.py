"""Tests for the Citation, Claim and Ticket models in registry/common_schemas.py."""

import pytest
from pydantic import ValidationError

from registry.common_schemas import Citation, Claim, Ticket

CITATION = {
    "url": "https://docs.python.org/3/",
    "quote": "Python is easy to learn",
    "retrieved_at": "2026-11-07T10:00:00Z",
}


def test_a_citation_parses_its_timestamp() -> None:
    citation = Citation.model_validate(CITATION)
    assert citation.retrieved_at.year == 2026


@pytest.mark.parametrize(
    "bad",
    [{"url": "ftp://x/y"}, {"url": ""}, {"quote": ""}, {"retrieved_at": "yesterday"}],
)
def test_a_citation_rejects_a_bad_field(bad: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Citation.model_validate({**CITATION, **bad})


def test_a_citation_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        Citation.model_validate({**CITATION, "page": 3})


def test_a_fact_needs_a_citation() -> None:
    with pytest.raises(ValidationError, match="at least one citation"):
        Claim(text="Python is easy.", fact=True, citations=[])


def test_a_fact_with_a_citation_is_valid() -> None:
    claim = Claim.model_validate({"text": "Python is easy.", "fact": True, "citations": [CITATION]})
    assert len(claim.citations) == 1


def test_a_claim_that_is_not_a_fact_may_cite_nothing() -> None:
    assert Claim(text="Prefer Python.", fact=False, citations=[]).citations == []


def test_the_claim_schema_is_a_plain_json_schema() -> None:
    schema = Claim.model_json_schema()
    assert set(schema["required"]) == {"text", "fact", "citations"}


TICKET = {
    "kind": "bug",
    "title": "Fix the parser",
    "body": "It drops the last line.",
    "acceptance": ["the last line parses"],
    "repo": "agb1986/agb-agent-factory",
    "ref": "main",
}


def test_a_ticket_has_exactly_the_agent_visible_fields() -> None:
    assert set(Ticket.model_fields) == {
        "kind", "title", "body", "acceptance", "repo", "ref", "impacted_paths",
    }  # fmt: skip
    assert Ticket.model_validate(TICKET).impacted_paths == []


@pytest.mark.parametrize("repo", ["agb1986/agb-agent-factory", "/home/me/repo"])
def test_a_ticket_takes_a_github_name_or_an_absolute_path(repo: str) -> None:
    assert Ticket.model_validate({**TICKET, "repo": repo}).repo == repo


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "chore"},
        {"title": ""},
        {"title": "two\nlines"},
        {"title": "   "},
        {"title": "x" * 121},
        {"acceptance": []},
        {"acceptance": [" "]},
        {"repo": "just-a-name"},
        {"repo": "a/b/c"},
        {"repo": "relative/path with space"},
        {"ref": ""},
        {"impacted_paths": ["/etc/passwd"]},
        {"impacted_paths": [""]},
        {"status": "todo"},
        {"depends_on": []},
    ],
)
def test_a_ticket_rejects_a_bad_or_unknown_field(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Ticket.model_validate({**TICKET, **bad})
