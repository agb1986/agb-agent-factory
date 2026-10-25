"""Models shared by the agents that change a work tree: the test-writer and the fixer.

Both report what they did the same way: the files they wrote and the commands they ran. The output
models are sent to the Messages API as a JSON schema (`output_config.format`), so every field is a
plain type, a `Literal` or a nested model. An agent's `schemas.py` is imported by file path and
cannot import a sibling agent's module, so the shared pieces live here and each `schemas.py`
re-exports them under the names its manifest uses.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FileAction = Literal["created", "modified", "deleted"]


class FileChange(BaseModel):
    """One file the agent wrote, with what it did to it."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Path relative to the repository root.")
    action: FileAction = Field(description="created, modified or deleted.")
    summary: str = Field(description="One line on what the change does.")


class CommandRun(BaseModel):
    """One command the agent ran in the sandbox, in the order it ran it."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(description="The program and its arguments, one per item.")
    exit_code: int = Field(description="The exit code the sandbox reported.")
    output: str = Field(
        description="The last lines of the command's output, enough to show why it passed or failed."
    )
