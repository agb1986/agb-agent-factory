"""Input and output models of the technical-feasibility agent.

The agent answers one question: can this idea be built, in this repository and with these
libraries? It reads the repository and the documentation of the ecosystem and returns a verdict,
the reasons for it as claims (`registry.common_schemas.Claim`), each fact cited with a passage
copied from the page that says it, the constraints it found, and a spike when the documentation
leaves the answer open.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from registry.common_schemas import Citation, Claim

FeasibilityVerdict = Literal["viable", "viable_with_constraints", "not_viable", "unknown"]


class FeasibilityInput(BaseModel):
    """The question to answer and the repository it is asked about."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    ref: str | None = Field(
        default=None,
        min_length=1,
        description="Git ref to read the repository at; null for a question only about libraries.",
    )
    question: str = Field(min_length=1, description="The feasibility question, as asked.")


class Spike(BaseModel):
    """A short, throwaway experiment that would settle what the sources leave open."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="A short name for the experiment.")
    establishes: str = Field(
        min_length=1, description="What running it would establish, and how to read the outcome."
    )


class FeasibilityOutput(BaseModel):
    """A feasibility judgement: the verdict, its reasons, the constraints and an optional spike."""

    model_config = ConfigDict(extra="forbid")

    verdict: FeasibilityVerdict = Field(
        description="viable, viable_with_constraints, not_viable, or unknown when unsettled."
    )
    summary: str = Field(min_length=1, description="Two or three sentences answering the question.")
    reasons: list[Claim] = Field(
        description="Why the verdict holds; every fact cites a passage copied from its page."
    )
    constraints: list[str] = Field(
        description="Conditions the idea is viable only under, each naming its source."
    )
    spike: Spike | None = Field(
        description="An experiment that would settle an open point, or null when nothing is open."
    )


__all__ = [
    "Citation",
    "Claim",
    "FeasibilityInput",
    "FeasibilityOutput",
    "FeasibilityVerdict",
    "Spike",
]
