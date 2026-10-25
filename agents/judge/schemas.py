"""Input and output models of the judge agent.

The judge reads the reviews a panel of reviewers wrote about one change, checks their findings
against the repository, and returns four scores, the merged findings and a rationale. It never
decides whether the change ships: `runtime.scoring` weighs the scores with the manifest's `scoring`
block and compares the result with its threshold, so neither number ever reaches the model.

The review models come from `registry.common_schemas`, the contract every reviewer shares.
"""

import re
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from registry.common_schemas import Finding, ReviewOutput
from registry.spec import NAME_PATTERN, VERSION_PATTERN
from tools import MAX_OUTPUT_BYTES

ADDRESS_PATTERN = rf"^{NAME_PATTERN.strip('^$')}@{VERSION_PATTERN.strip('^$')}$"
"""A resolved agent address, `name@MAJOR.MINOR.PATCH`, as trace rows and reviews carry it."""

type ReviewerAddress = Annotated[str, Field(pattern=ADDRESS_PATTERN)]

_TRUNCATION_MARKER = re.compile(r"\n?\[truncated: \d+ bytes dropped, limit \d+ bytes\]\Z")


class DiffContext(BaseModel):
    """The change the reviews are about: its two refs and the unified diff between them."""

    model_config = ConfigDict(extra="forbid")

    base: str = Field(description="Git ref of the revision the change starts from.")
    head: str = Field(description="Git ref of the revision under review.")
    diff: str = Field(
        description=(
            "Unified diff from base to head, as git.show_diff returns it: at most the runtime's "
            "tool output limit, with a final truncation marker when it was cut."
        )
    )

    @field_validator("diff")
    @classmethod
    def _within_output_limit(cls, value: str) -> str:
        """Reject a diff longer than `tools.MAX_OUTPUT_BYTES`, not counting a truncation marker."""
        size = len(_TRUNCATION_MARKER.sub("", value).encode("utf-8"))
        if size > MAX_OUTPUT_BYTES:
            raise ValueError(
                f"diff is {size} bytes, over the {MAX_OUTPUT_BYTES}-byte limit; "
                "cap it the way git.show_diff does"
            )
        return value


class FailedReviewer(BaseModel):
    """A reviewer that ran and failed, so its part of the change went unreviewed."""

    model_config = ConfigDict(extra="forbid")

    reviewer: ReviewerAddress = Field(
        description="Address of the reviewer, such as security-reviewer@1.0.0."
    )
    error: str = Field(min_length=1, description="The error that ended its run.")


class JudgeInput(BaseModel):
    """What to judge: the panel's finished reviews, the reviewers that failed, and the change.

    Every reviewer appears once, either with a review or as failed. At least one review is required;
    a panel that produced none has nothing to judge.
    """

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools read from.")
    reviews: dict[ReviewerAddress, ReviewOutput] = Field(
        min_length=1,
        description="Each finished review, keyed by its reviewer's address, such as "
        "code-reviewer@1.2.0.",
    )
    failed_reviewers: list[FailedReviewer] = Field(
        default_factory=list, description="Reviewers that failed; empty when every one finished."
    )
    diff_context: DiffContext

    @model_validator(mode="after")
    def _each_reviewer_once(self) -> Self:
        seen = set(self.reviews)
        for failed in self.failed_reviewers:
            if failed.reviewer in seen:
                raise ValueError(f"reviewer {failed.reviewer} is listed more than once")
            seen.add(failed.reviewer)
        return self


class Score(BaseModel):
    """One score from 0 to 1 and the line that justifies it."""

    model_config = ConfigDict(extra="forbid")

    value: float = Field(ge=0, le=1, description="The score, from 0 to 1.")
    justification: str = Field(description="One line naming the evidence that decided the score.")


class Scores(BaseModel):
    """The four scores. The two risks are high when bad; the other two are high when good."""

    model_config = ConfigDict(extra="forbid")

    correctness_risk: Score = Field(
        description="How likely the change behaves wrongly: 0 for no sign of a defect, "
        "1 for a confirmed defect on a common path."
    )
    maintainability: Score = Field(
        description="How easy the changed code is to read and change safely: 1 for clear code, "
        "0 for a trap the next author will fall into."
    )
    security_risk: Score = Field(
        description="How likely the change opens a security hole: 0 for none, "
        "1 for a confirmed exploitable one."
    )
    test_coverage: Score = Field(
        description="How well tests exercise the changed behaviour: 1 for every changed branch, "
        "0 for none of it."
    )


class MergedFinding(Finding):
    """A finding after the judge checked it, with every reviewer that reported it."""

    sources: list[str] = Field(
        min_length=1,
        description="Addresses of the reviewers that reported this finding, such as "
        "code-reviewer@1.2.0.",
    )


class JudgeOutput(BaseModel):
    """The judgement: four scores, the merged findings and the reasoning behind them."""

    model_config = ConfigDict(extra="forbid")

    scores: Scores
    merged_findings: list[MergedFinding] = Field(
        description="The reviewers' findings after checking, one per defect; empty when none "
        "survives."
    )
    rationale: str = Field(
        description="What decided the scores, which findings were dropped or downgraded and why, "
        "and any blind spot a failed reviewer left."
    )


__all__ = [
    "ADDRESS_PATTERN",
    "DiffContext",
    "FailedReviewer",
    "JudgeInput",
    "JudgeOutput",
    "MergedFinding",
    "ReviewerAddress",
    "Score",
    "Scores",
]
