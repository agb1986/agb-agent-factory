"""Input and output models of the test-writer agent.

The shared pieces (`FileChange`, `CommandRun`, `CoverageReport`) live in `registry.work_schemas`;
the review `Finding` lives in `registry.common_schemas`.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from registry.common_schemas import Finding
from registry.work_schemas import CommandRun, CoverageReport, FileChange, FileCoverage


class TestWriterInput(BaseModel):
    """What to test in the repository at `repo_path`, in one of two modes.

    `reproduce` (the default) writes a test that fails because of a reviewer's `finding`. `cover`
    writes passing tests for the lines of `target_paths` that `coverage` reports as missing.
    """

    __test__ = False

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    mode: Literal["reproduce", "cover"] = Field(
        default="reproduce",
        description="reproduce: write a test that fails because of the finding. "
        "cover: write passing tests for the missing lines of the target paths.",
    )
    finding: Finding | None = Field(
        default=None,
        description="The defect, as a code reviewer reported it. Given in reproduce mode only.",
    )
    target_paths: list[str] = Field(
        default_factory=list,
        description="The source files to cover, relative to the repository root. "
        "Given in cover mode only.",
    )
    coverage: CoverageReport | None = Field(
        default=None,
        description="The line coverage of the target paths under the current test suite. "
        "Given in cover mode only.",
    )

    @model_validator(mode="after")
    def _fields_match_mode(self) -> Self:
        if self.mode == "reproduce":
            if self.finding is None:
                raise ValueError("reproduce mode needs a finding")
            if self.target_paths or self.coverage is not None:
                raise ValueError("target_paths and coverage are read in cover mode only")
            return self
        if self.finding is not None:
            raise ValueError("cover mode takes no finding")
        if not self.target_paths or self.coverage is None:
            raise ValueError("cover mode needs target_paths and coverage")
        reported = [entry.path for entry in self.coverage.files]
        if sorted(reported) != sorted(self.target_paths):
            raise ValueError(
                f"coverage reports {reported}, which is not the target paths {self.target_paths}"
            )
        return self


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


__all__ = [
    "CommandRun",
    "CoverageReport",
    "FileChange",
    "FileCoverage",
    "Finding",
    "TestWriterInput",
    "TestWriterOutput",
]
