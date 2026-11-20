"""Input and output models of the refactorer agent.

The ticket is `registry.common_schemas.Ticket` with kind `refactor` (ADR 0002): the targets are its
`impacted_paths`, the smell or deprecated API and the standard to apply are in its `body`, and what
must still hold is its `acceptance`. The shared pieces of the answer (`FileChange`, `CommandRun`)
live in `registry.work_schemas`.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from registry.common_schemas import Ticket
from registry.work_schemas import CommandRun, FileChange


class RefactorerInput(BaseModel):
    """The refactor ticket, and what earlier work it builds on.

    It carries no status, cost or store id: the work-item store keeps those, and the agent needs
    none of them to change the code.
    """

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    ticket: Ticket = Field(
        description="What to refactor: the target files in `impacted_paths`, the smell or "
        "deprecated API and the standard to apply in `body`, and what must still hold in "
        "`acceptance`."
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="One line per finished work item this ticket builds on, saying what it "
        "added. Empty when there is none or it is not known.",
    )

    @model_validator(mode="after")
    def _ticket_is_a_refactor(self) -> Self:
        if self.ticket.kind != "refactor":
            raise ValueError(f"the refactorer takes refactor tickets, not {self.ticket.kind!r}")
        return self


class RefactorerOutput(BaseModel):
    """What the refactorer changed and what it kept the same, or why it stopped."""

    model_config = ConfigDict(extra="forbid")

    files_changed: list[FileChange] = Field(
        description="Every production file written. Never a test file. Empty when blocked."
    )
    commands_run: list[CommandRun] = Field(description="Every sandbox command, in order.")
    summary: str = Field(description="What was changed, where, and to which standard.")
    behaviour_kept: str = Field(
        description="What stayed the same: the public names and signatures of the targets, "
        "the behaviour the suite pins, and the suite result before and after. When blocked, "
        "what was checked before stopping."
    )
    signatures_changed: list[str] = Field(
        description="Each public name whose signature changed, as `path::name`, which the "
        "ticket must name. Empty when none changed, which is the usual case."
    )
    blocked: bool = Field(
        description="True when the refactorer stopped without changing a file: the suite was "
        "not green at the start, or the refactor cannot be done without changing behaviour, a "
        "test, or a public signature the ticket does not name."
    )
    blocked_reason: str | None = Field(
        description="When `blocked`, what stopped the work, with the test or the file and line "
        "that shows it; null otherwise."
    )

    @model_validator(mode="after")
    def _blocked_has_a_reason(self) -> Self:
        if self.blocked and not (self.blocked_reason or "").strip():
            raise ValueError("a blocked answer needs a blocked_reason")
        if not self.blocked and self.blocked_reason is not None:
            raise ValueError("blocked_reason is only set when blocked")
        if self.blocked and self.files_changed:
            raise ValueError("a blocked answer changes no file")
        return self


__all__ = [
    "CommandRun",
    "FileChange",
    "RefactorerInput",
    "RefactorerOutput",
    "Ticket",
]
