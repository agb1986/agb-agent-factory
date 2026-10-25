"""Input and output models of the fixer agent.

The shared pieces (`FileChange`, `CommandRun`) live in `registry.work_schemas`; the review
`Finding` lives in `registry.common_schemas`.
"""

from pydantic import BaseModel, ConfigDict, Field

from registry.common_schemas import Finding
from registry.work_schemas import CommandRun, FileChange


class FixerInput(BaseModel):
    """The defect to fix, and the tests that show it."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    finding: Finding = Field(description="The defect, as a code reviewer reported it.")
    test_files: list[str] = Field(
        description="Test files that cover the defect, relative to the repository root. "
        "They are expected to fail until the defect is fixed."
    )


class FixerOutput(BaseModel):
    """What the fixer did to production code, and whether the suite passes."""

    model_config = ConfigDict(extra="forbid")

    files_changed: list[FileChange] = Field(
        description="Every file written, production code only. Empty when nothing needed fixing."
    )
    commands_run: list[CommandRun] = Field(description="Every sandbox command, in order.")
    nothing_to_fix: bool = Field(
        description="True when the tests pass on the unchanged tree and the finding describes no "
        "defect the code has, so no change was made."
    )
    suite_passes: bool = Field(
        description="True when the last full-suite run, `python -m pytest -q`, exited 0."
    )
    summary: str = Field(description="The cause, the change made, or why nothing needed fixing.")


__all__ = ["CommandRun", "FileChange", "Finding", "FixerInput", "FixerOutput"]
