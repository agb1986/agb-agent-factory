"""Input and output models of the test-writer agent.

The shared pieces (`FileChange`, `CommandRun`) live in `registry.work_schemas`; the review
`Finding` lives in `registry.common_schemas`.
"""

from pydantic import BaseModel, ConfigDict, Field

from registry.common_schemas import Finding
from registry.work_schemas import CommandRun, FileChange


class TestWriterInput(BaseModel):
    """The defect to reproduce: a reviewer's `finding` in the repository at `repo_path`."""

    __test__ = False

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    finding: Finding = Field(description="The defect, as a code reviewer reported it.")


class TestWriterOutput(BaseModel):
    """What the test-writer did and what it expects of the tests it wrote."""

    __test__ = False

    model_config = ConfigDict(extra="forbid")

    files_changed: list[FileChange] = Field(description="Every file written, test files only.")
    commands_run: list[CommandRun] = Field(description="Every sandbox command, in order.")
    test_files: list[str] = Field(
        description="Paths of the test files holding the new tests, relative to the repository root."
    )
    fails_on_head: bool = Field(
        description="True when the new tests fail on the current tree, as the last run showed. "
        "False when they pass, as for tests that only add coverage."
    )
    summary: str = Field(description="What the tests check and what the last run showed.")


__all__ = ["CommandRun", "FileChange", "Finding", "TestWriterInput", "TestWriterOutput"]
