"""Input and output models of the benchmark agent.

The agent compares candidate libraries for one need on five fixed criteria and recommends one.
Every cell of the comparison table is a `Claim` (`registry.common_schemas`), so a number can only
reach the output inside a claim marked as a fact with a citation whose quote is copied from the
page; a criterion the sources do not cover is a claim marked `fact: false` that says it is not
published. No field of the output is a number type, so a figure has nowhere else to go.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from registry.common_schemas import Citation, Claim

Criterion = Literal["licence", "maintenance", "performance", "size", "language_support"]
CRITERIA: tuple[Criterion, ...] = (
    "licence",
    "maintenance",
    "performance",
    "size",
    "language_support",
)
"""The fixed criteria, in the order the table lists them for each candidate."""


class CandidateRef(BaseModel):
    """A candidate the requester already has in mind: its name and, optionally, its page."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="The library's name, as it is published.")
    url: str | None = Field(
        default=None,
        pattern=r"^https?://",
        description="A page about the library, such as its PyPI or project page, when known.",
    )


class BenchmarkInput(BaseModel):
    """The need to meet, the candidates to compare, and the repository the need comes from."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    ref: str = Field(default="HEAD", min_length=1, description="The git ref to read files at.")
    need: str = Field(
        min_length=1, description="What the library must do, and any hard constraint on it."
    )
    candidates: list[CandidateRef] = Field(
        default_factory=list,
        description="Libraries to compare; when empty the agent looks for them with web.search.",
    )


class Cell(Claim):
    """One cell of the comparison table: what the sources say about one criterion of a candidate.

    It is a `Claim`, so `fact`, `citations` and the rule that a fact needs a citation are the same
    as for any research output, and `citations_resolve` checks it as one.
    """

    candidate: str = Field(min_length=1, description="The candidate's name, as in `candidates`.")
    criterion: Criterion = Field(
        description="licence, maintenance, performance, size or language_support."
    )


class Candidate(BaseModel):
    """One library compared, how it came to be compared, and where it ended up."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="The library's name, as it is published.")
    url: str | None = Field(
        default=None, pattern=r"^https?://", description="The page it was read from, if any."
    )
    found_by: Literal["given", "search"] = Field(
        description="given when the input named it, search when web.search found it."
    )
    status: Literal["recommended", "viable", "ruled_out"] = Field(
        description="recommended for the one pick, viable for a fallback, ruled_out otherwise."
    )
    ruled_out_because: str | None = Field(
        default=None, description="Why it cannot meet the need; required when ruled_out."
    )

    @model_validator(mode="after")
    def _ruled_out_has_a_reason(self) -> Self:
        if self.status == "ruled_out" and not (self.ruled_out_because or "").strip():
            raise ValueError("a ruled_out candidate needs ruled_out_because")
        return self


class Spike(BaseModel):
    """A short experiment that would produce the number the decision hinges on."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="A short name for the experiment.")
    measures: str = Field(
        min_length=1,
        description="What it measures, on which candidates, under which conditions, and which "
        "result would pick which candidate.",
    )


class BenchmarkOutput(BaseModel):
    """The comparison: the candidates, the cited table, the pick and what it rests on."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[Candidate] = Field(
        description="Every library compared; empty only when none could be found."
    )
    table: list[Cell] = Field(
        description="One cell per candidate per criterion; facts carry citations, unknowns "
        "are fact false and say not published."
    )
    recommendation: str = Field(
        min_length=1,
        description="Which candidate to use and why, or why none can be recommended yet.",
    )
    depends_on: list[str] = Field(
        description="The facts or assumptions the recommendation rests on, one per entry."
    )
    spike: Spike | None = Field(
        description="The experiment to run when the decision hinges on an unpublished number; "
        "null otherwise."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = {candidate.name for candidate in self.candidates}
        unknown = sorted({cell.candidate for cell in self.table} - names)
        if unknown:
            raise ValueError(f"table cells name candidates not in candidates: {unknown}")
        if sum(candidate.status == "recommended" for candidate in self.candidates) > 1:
            raise ValueError("at most one candidate is recommended")
        return self


__all__ = [
    "CRITERIA",
    "BenchmarkInput",
    "BenchmarkOutput",
    "Candidate",
    "CandidateRef",
    "Cell",
    "Citation",
    "Criterion",
    "Spike",
]
