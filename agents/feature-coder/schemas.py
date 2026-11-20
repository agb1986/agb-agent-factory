"""Input and output models of the feature-coder agent.

The ticket is `registry.common_schemas.Ticket`, the shape every agent sees a work item in; the
shared pieces of the answer (`FileChange`, `CommandRun`) live in `registry.work_schemas`.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from registry.common_schemas import Ticket
from registry.work_schemas import CommandRun, FileChange


class FeatureInput(BaseModel):
    """The ticket to build, and what earlier work it builds on.

    It carries no status, cost or store id: the work-item store keeps those, and the agent needs
    none of them to write the code.
    """

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    ticket: Ticket = Field(
        description="What to build: title, body, acceptance criteria, repository and ref, and "
        "the impacted paths when known."
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="One line per finished work item this ticket builds on, saying what it "
        "added. Empty when there is none or it is not known.",
    )


class AcceptanceCheck(BaseModel):
    """One acceptance criterion of the ticket, and the tests that check it."""

    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(description="The criterion, as the ticket states it.")
    tests: list[str] = Field(
        description="The pytest node ids that check it, such as "
        "`tests/test_stock.py::test_reserve_rejects_more_than_the_limit`. Empty only when the "
        "criterion cannot be checked by a test."
    )
    untestable_reason: str | None = Field(
        description="Why no test can check the criterion; null when `tests` is not empty."
    )

    @model_validator(mode="after")
    def _tested_or_explained(self) -> Self:
        if not self.tests and not self.untestable_reason:
            raise ValueError("a criterion with no test needs an untestable_reason")
        if self.tests and self.untestable_reason:
            raise ValueError("a criterion with tests takes no untestable_reason")
        return self


class FeatureOutput(BaseModel):
    """What the coder changed, the tests that check the ticket, or why it stopped."""

    model_config = ConfigDict(extra="forbid")

    files_changed: list[FileChange] = Field(
        description="Every file written, code and tests. Empty when the coder is blocked."
    )
    test_files: list[str] = Field(
        description="The test files that check the acceptance criteria, relative to the "
        "repository root. Empty when the coder is blocked."
    )
    acceptance: list[AcceptanceCheck] = Field(
        description="Each acceptance criterion of the ticket, in order, with its tests. Empty "
        "when the coder is blocked."
    )
    commands_run: list[CommandRun] = Field(description="Every sandbox command, in order.")
    summary: str = Field(description="What was built and how it was checked, or what was found.")
    open_questions: list[str] = Field(
        description="Questions for whoever owns the ticket that the work did not need answered "
        "but that a reader should see. Empty when there are none."
    )
    blocked: bool = Field(
        description="True when the coder stopped without building the ticket, because its "
        "acceptance criteria contradict the code or each other."
    )
    blocked_reason: str | None = Field(
        description="When `blocked`, the criterion and the code or criterion it contradicts; "
        "null otherwise."
    )

    @model_validator(mode="after")
    def _blocked_has_a_reason(self) -> Self:
        if self.blocked and not (self.blocked_reason or "").strip():
            raise ValueError("a blocked answer needs a blocked_reason")
        if not self.blocked and self.blocked_reason is not None:
            raise ValueError("blocked_reason is only set when blocked")
        return self


__all__ = [
    "AcceptanceCheck",
    "CommandRun",
    "FeatureInput",
    "FeatureOutput",
    "FileChange",
    "Ticket",
]
