"""Input and output models of the documentation agent.

The ticket is `registry.common_schemas.Ticket` with kind `docs` (ADR 0002): the change to document
is named in its `body`, as a diff range such as `base..head` or as the ticket that made it, the
files it touched are its `impacted_paths`, and what the docs must say afterwards is its
`acceptance`. The build loop calls the agent as the `docs` kind's coder, with the same input every
coder takes, and reads `files_changed`, `summary`, `blocked` and `blocked_reason` from its answer.
The shared pieces of the answer (`FileChange`, `CommandRun`) live in `registry.work_schemas`.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from registry.common_schemas import Ticket
from registry.work_schemas import CommandRun, FileChange


class DocumentationAgentInput(BaseModel):
    """The docs ticket, and what earlier work it builds on.

    It carries no status, cost or store id: the work-item store keeps those, and the agent needs
    none of them to write the docs.
    """

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    ticket: Ticket = Field(
        description="What to document: the change in `body`, as a diff range or the ticket that "
        "made it, the files it touched in `impacted_paths`, and what the docs must say in "
        "`acceptance`."
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="One line per finished work item this ticket builds on, saying what it "
        "added. Empty when there is none or it is not known.",
    )

    @model_validator(mode="after")
    def _ticket_is_docs(self) -> Self:
        if self.ticket.kind != "docs":
            raise ValueError(
                f"the documentation agent takes docs tickets, not {self.ticket.kind!r}"
            )
        return self


class DocChange(BaseModel):
    """One piece of documentation the agent wrote, and the code change that called for it."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="The file written, relative to the repository root.")
    what: str = Field(
        description="What changed in it: the section, docstring or comment, and what it now says."
    )
    why: str = Field(
        description="The code change that made it needed, such as a new function, a renamed "
        "name or a new module, with the file it is in."
    )


class DocumentationAgentOutput(BaseModel):
    """The docs the agent wrote and why, or why it stopped."""

    model_config = ConfigDict(extra="forbid")

    files_changed: list[FileChange] = Field(
        description="Every file written: docs, or source files changed only in their docstrings "
        "and comments. Never a test file. Empty when blocked."
    )
    commands_run: list[CommandRun] = Field(description="Every sandbox command, in order.")
    summary: str = Field(
        description="What the docs now cover, where, and which change they follow."
    )
    docs_changed: list[DocChange] = Field(
        description="One entry per docs change, with the file, what it says now and why it was "
        "needed. Empty when blocked."
    )
    blocked: bool = Field(
        description="True when the agent stopped without changing a file: the change to document "
        "cannot be found, or documenting it would need a code change."
    )
    blocked_reason: str | None = Field(
        description="When `blocked`, what stopped the work, with the file and line or the ref "
        "that shows it; null otherwise."
    )

    @model_validator(mode="after")
    def _blocked_has_a_reason(self) -> Self:
        if self.blocked and not (self.blocked_reason or "").strip():
            raise ValueError("a blocked answer needs a blocked_reason")
        if not self.blocked and self.blocked_reason is not None:
            raise ValueError("blocked_reason is only set when blocked")
        if self.blocked and (self.files_changed or self.docs_changed):
            raise ValueError("a blocked answer changes no file")
        return self


__all__ = [
    "CommandRun",
    "DocChange",
    "DocumentationAgentInput",
    "DocumentationAgentOutput",
    "FileChange",
    "Ticket",
]
