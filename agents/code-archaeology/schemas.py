"""Input and output models of the code-archaeology agent.

The output is `CodeArchaeologyOutput` of `registry/decomposition_schemas.py`, which the agent that
plans an extraction takes as part of its input. The `ground_truth` eval grader
(`evals/graders/ground_truth.py`) reads its `dead_code`, `data_flows` and `execution_paths`.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from registry.decomposition_schemas import SYMBOL_PATTERN, CodeArchaeologyOutput

__all__ = ["CodeArchaeologyInput", "CodeArchaeologyOutput"]

_EntryPoint = Annotated[str, StringConstraints(max_length=160, pattern=SYMBOL_PATTERN)]


class CodeArchaeologyInput(BaseModel):
    """The repository to read and the symbol to start from."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools read from.")
    entry_point: _EntryPoint = Field(
        description="The Python symbol to start from, `path::Qualified.name` relative to the "
        "repository root; a path alone is the module's top-level code."
    )

    @field_validator("entry_point")
    @classmethod
    def _inside_the_repository(cls, value: str) -> str:
        if ".." in value.partition("::")[0].split("/"):
            raise ValueError("entry_point must be relative to the repository root, without '..'")
        return value
