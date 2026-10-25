"""Pydantic models for agent manifests: `AgentSpecBase`, `LLMSpec` and the `JevSpec` placeholder.

The field set, its meaning and the layout of `agent.yaml` are fixed in `docs/CONVENTIONS.md`. Every
model forbids unknown keys, so a typo in a manifest is an error and never falls back to a default.
Loading a manifest from disk, and importing the schemas module an `LLMSpec` names, is the job of
`registry.resolver`; this module only describes and validates the data.
"""

from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

NAME_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
VERSION_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*\.[a-z][a-z0-9]*(_[a-z0-9]+)*$"

type Name = Annotated[str, Field(pattern=NAME_PATTERN)]
type Version = Annotated[str, Field(pattern=VERSION_PATTERN)]
type ToolName = Annotated[str, Field(pattern=TOOL_NAME_PATTERN)]


class _Model(BaseModel):
    """Base for every manifest block: unknown keys are errors and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalsSpec(_Model):
    """The `evals` block: the pass rate the publish gate requires, from 0 to 1."""

    min_pass_rate: float = Field(ge=0, le=1)


class JevEvalsSpec(EvalsSpec):
    """The `evals` block of a Jev agent, which may also set the recall floor for risky diffs."""

    min_recall_risky: float | None = Field(default=None, ge=0, le=1)


class ToolsSpec(_Model):
    """The `tools` block: dotted tool names an agent may use and ones it must never use.

    An absent block means no tools. A name in both lists is an error.
    """

    allow: tuple[ToolName, ...] = ()
    deny: tuple[ToolName, ...] = ()

    @model_validator(mode="after")
    def _no_overlap(self) -> ToolsSpec:
        both = sorted(set(self.allow) & set(self.deny))
        if both:
            raise ValueError(f"tools in both allow and deny: {', '.join(both)}")
        return self


class LimitsSpec(_Model):
    """The `limits` block: hard caps on one run. All three are required and positive."""

    max_tool_turns: int = Field(gt=0)
    timeout_s: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class AgentSpecBase(_Model):
    """Fields shared by every agent kind.

    `path` is the manifest file the spec was loaded from. It is set by the loader and is not a
    manifest key. `version` is a full `MAJOR.MINOR.PATCH`.
    """

    name: Name
    version: Version
    kind: str
    description: str = Field(min_length=1)
    evals: EvalsSpec
    path: Path

    @property
    def address(self) -> str:
        """The resolved form `name@MAJOR.MINOR.PATCH` used in trace rows and tags."""
        return f"{self.name}@{self.version}"

    @property
    def directory(self) -> Path:
        """The directory holding the manifest; relative paths in the manifest start here."""
        return self.path.parent


class LLMSpec(AgentSpecBase):
    """An agent that reasons, may call scoped tools, and returns a Pydantic model.

    `input_model` and `output_model` are the class names from the manifest. The loaded classes are
    `input_class` and `output_class`, available once the loader has called `attach_models`.
    """

    kind: Literal["llm"]
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    system_prompt: Path
    schemas: Path
    input_model: str = Field(min_length=1)
    output_model: str = Field(min_length=1)
    tools: ToolsSpec = ToolsSpec()
    limits: LimitsSpec

    _input_class: type[BaseModel] | None = PrivateAttr(default=None)
    _output_class: type[BaseModel] | None = PrivateAttr(default=None)

    @field_validator("system_prompt", "schemas")
    @classmethod
    def _inside_directory(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in PurePosixPath(value.as_posix()).parts:
            raise ValueError("must be a relative path inside the agent directory")
        return value

    @property
    def system_prompt_file(self) -> Path:
        """Absolute-from-the-manifest path of the system prompt file."""
        return self.directory / self.system_prompt

    @property
    def schemas_file(self) -> Path:
        """Path of the module that defines the input and output models."""
        return self.directory / self.schemas

    @property
    def input_class(self) -> type[BaseModel]:
        """The loaded input model class; raises `RuntimeError` before `attach_models`."""
        if self._input_class is None:
            raise RuntimeError(f"{self.address}: models are not loaded; use registry.resolve()")
        return self._input_class

    @property
    def output_class(self) -> type[BaseModel]:
        """The loaded output model class; raises `RuntimeError` before `attach_models`."""
        if self._output_class is None:
            raise RuntimeError(f"{self.address}: models are not loaded; use registry.resolve()")
        return self._output_class

    def attach_models(self, input_class: type[BaseModel], output_class: type[BaseModel]) -> None:
        """Record the classes the loader imported from `schemas`. Called by the loader only."""
        self._input_class = input_class
        self._output_class = output_class


class JevSpec(AgentSpecBase):
    """Placeholder for a Jev agent: shared fields are validated, the rest is accepted as given.

    The Jev-specific keys listed in `docs/CONVENTIONS.md` are accepted untyped. #16 replaces them
    with real models and validation.
    """

    kind: Literal["jev"]
    evals: JevEvalsSpec
    model: str | None = None
    input: dict[str, Any] | None = None
    questions: list[dict[str, Any]] | None = None
    gates: list[dict[str, Any]] | None = None
    routing: list[dict[str, Any]] | None = None


AgentSpec = LLMSpec | JevSpec

KINDS = ("llm", "jev")
