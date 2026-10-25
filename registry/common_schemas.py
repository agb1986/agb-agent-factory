"""Review models shared by every reviewer agent.

The output model is sent to the Messages API as a JSON schema (`output_config.format`), so every
field is a plain type, a `Literal` or a nested model. Constraints the API cannot enforce, such as
`ge=1`, are checked again by Pydantic when the reply is parsed, and a failure costs the run its
one validation retry.

An agent's `schemas.py` is imported by file path, so it cannot import a sibling agent's module.
Reviewers that share the review contract import these models from here and re-export them under
the names their manifest's `input_model` and `output_model` give. The judge consumes `Finding`
when it merges the panel's reviews.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Severity = Literal["info", "minor", "major", "blocker"]
Verdict = Literal["approve", "request_changes"]


class ReviewInput(BaseModel):
    """What to review: the commits `base` and `head` of the repository at `repo_path`."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools read from.")
    base: str = Field(description="Git ref of the revision the change starts from.")
    head: str = Field(description="Git ref of the revision under review.")
    focus: str | None = Field(
        default=None,
        description="Optional area to look at first, such as 'error handling' or a file path.",
    )


class Finding(BaseModel):
    """One defect or concern, located in the head revision."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="Path relative to the repository root, as it is at head.")
    line_start: int = Field(ge=1, description="First line of the code at fault, in the head file.")
    line_end: int = Field(
        ge=1, description="Last line of the code at fault; equal to line_start for a single line."
    )
    severity: Severity = Field(description="info, minor, major or blocker.")
    category: str = Field(description="Short lowercase label, such as logic, security or testing.")
    title: str = Field(description="One line naming the problem.")
    body: str = Field(description="What is wrong, why it matters, and how it fails.")
    suggested_fix: str | None = Field(
        default=None, description="A concrete fix, when one is clear."
    )

    @model_validator(mode="after")
    def _lines_in_order(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end must not be smaller than line_start")
        return self


class ReviewOutput(BaseModel):
    """The review: every finding, a short summary and an overall verdict."""

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding] = Field(description="Every finding; empty when the change is clean.")
    summary: str = Field(description="Two or three sentences on what the change does and its risk.")
    verdict: Verdict = Field(description="approve, or request_changes when any finding matters.")
