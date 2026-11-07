"""Tests for the Citation and Claim models in registry/common_schemas.py."""

import pytest
from pydantic import ValidationError

from registry.common_schemas import Citation, Claim

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
