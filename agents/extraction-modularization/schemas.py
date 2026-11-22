"""Input and output models of the extraction-modularization agent.

Part of the input is `CodeArchaeologyOutput`, the output of code-archaeology, and the output is
`ExtractionBlueprint`; both are in `registry/decomposition_schemas.py`. The `ground_truth` checks
(`evals/graders/ground_truth.py`) and `pipelines/decompose.py` read the blueprint.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from registry.decomposition_schemas import (
    SYMBOL_PATTERN,
    CodeArchaeologyOutput,
    ExtractionBlueprint,
)

__all__ = ["ExtractionBlueprint", "ExtractionModularizationInput", "FeatureRequest"]

_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
_Start = Annotated[str, StringConstraints(max_length=160, pattern=SYMBOL_PATTERN)]


class FeatureRequest(BaseModel):
    """The feature to draw the boundary of, as a person would name it."""

    model_config = ConfigDict(extra="forbid")

    name: _Name = Field(description="A short name for the feature.")
    start: _Start = Field(
        description="The Python symbol the feature starts at, `path::Qualified.name` relative to "
        "the repository root."
    )

    @field_validator("start")
    @classmethod
    def _inside_the_repository(cls, value: str) -> str:
        if ".." in value.partition("::")[0].split("/"):
            raise ValueError("start must be relative to the repository root, without '..'")
        return value


class ExtractionModularizationInput(BaseModel):
    """The repository, what archaeology found in it, and the feature to extract."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools read from.")
    archaeology: CodeArchaeologyOutput = Field(
        description="What code-archaeology found, read from one entry point."
    )
    feature: FeatureRequest = Field(description="The feature to draw the boundary of.")
