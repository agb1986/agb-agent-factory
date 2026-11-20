"""Pydantic models for agent manifests: `AgentSpecBase` and `LLMSpec`, and the shared blocks.

`JevSpec` lives in `registry.jev_spec`; it builds on the base classes defined here.

The field set, its meaning and the layout of `agent.yaml` are fixed in `docs/CONVENTIONS.md`. Every
model forbids unknown keys, so a typo in a manifest is an error and never falls back to a default.
Loading a manifest from disk, and importing the schemas module an `LLMSpec` names, is the job of
`registry.resolver`; this module only describes and validates the data.
"""

import math
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

NAME_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
VERSION_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*\.[a-z][a-z0-9]*(_[a-z0-9]+)*$"
DOMAIN_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
AGENT_RANGE_PATTERN = rf"{NAME_PATTERN[:-1]}@\^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
TEAMS = ("planning", "dev", "qa", "sre", "research", "decomp", "hitl", "analytics")
"""The eight `team` values, one per team of ADR 0002. A label only; see `AgentSpecBase.team`."""

type Name = Annotated[str, Field(pattern=NAME_PATTERN)]
type Version = Annotated[str, Field(pattern=VERSION_PATTERN)]
type ToolName = Annotated[str, Field(pattern=TOOL_NAME_PATTERN)]
type AgentRange = Annotated[str, Field(pattern=AGENT_RANGE_PATTERN)]
type Team = Literal["planning", "dev", "qa", "sre", "research", "decomp", "hitl", "analytics"]


class _Model(BaseModel):
    """Base for every manifest block: unknown keys are errors and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalsSpec(_Model):
    """The `evals` block: the pass rate the publish gate requires, from 0 to 1."""

    min_pass_rate: float = Field(ge=0, le=1)


class WebSpec(_Model):
    """The `tools.web` block: the domains the web tools may reach for this agent.

    `allow_domains` holds bare lowercase host names such as `docs.python.org`; a listed domain also
    allows its subdomains. Empty, the default, means the agent gets no web tools.
    """

    allow_domains: tuple[str, ...] = ()

    @field_validator("allow_domains")
    @classmethod
    def _bare_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for domain in value:
            if not re.fullmatch(DOMAIN_PATTERN, domain):
                raise ValueError(f"allow_domains entries are bare lowercase hosts, got {domain!r}")
        return value


class ToolsSpec(_Model):
    """The `tools` block: dotted tool names an agent may use and ones it must never use.

    An absent block means no tools. A name in both lists is an error.
    """

    allow: tuple[ToolName, ...] = ()
    deny: tuple[ToolName, ...] = ()
    web: WebSpec = WebSpec()

    @model_validator(mode="after")
    def _no_overlap(self) -> ToolsSpec:
        both = sorted(set(self.allow) & set(self.deny))
        if both:
            raise ValueError(f"tools in both allow and deny: {', '.join(both)}")
        return self


class FilesSpec(_Model):
    """The `files` block: the paths in the repository the agent's writes may and may not touch.

    Both lists hold glob patterns over paths relative to the repository root, matched with
    `PurePosixPath.full_match`: `*` matches within one path segment and `**` any number of whole
    segments, so `**/tests/**` is every path under a directory named `tests`. A path may be written
    when `allow` is empty or one of its patterns matches, and no `deny` pattern matches; `deny`
    wins. The pipeline that runs the agent enforces it on the host, in `pipelines.file_policy`,
    on every `fs.write_file` and again on what changed after the run. A pattern that is absolute,
    climbs out with `..` or is listed twice is an error.
    """

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @field_validator("allow", "deny")
    @classmethod
    def _relative_globs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            pure = PurePosixPath(pattern)
            if not pattern or pattern != pattern.strip() or "\\" in pattern:
                raise ValueError(f"file patterns are non-empty POSIX globs, got {pattern!r}")
            if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != pattern:
                raise ValueError(f"file patterns are relative to the repository, got {pattern!r}")
        repeated = sorted({pattern for pattern in value if value.count(pattern) > 1})
        if repeated:
            raise ValueError(f"file patterns listed twice: {', '.join(repeated)}")
        return value

    @model_validator(mode="after")
    def _no_overlap(self) -> FilesSpec:
        both = sorted(set(self.allow) & set(self.deny))
        if both:
            raise ValueError(f"file patterns in both allow and deny: {', '.join(both)}")
        return self

    def denial(self, path: str) -> str | None:
        """Return why `path`, relative to the repository root, may not be written, or None."""
        pure = PurePosixPath(path)
        for pattern in self.deny:
            if pure.full_match(pattern):
                return f"the path matches the files.deny pattern {pattern}"
        if self.allow and not any(pure.full_match(pattern) for pattern in self.allow):
            return "the path matches no files.allow pattern"
        return None


class LimitsSpec(_Model):
    """The `limits` block: hard caps on one run.

    The first three are required and positive. `max_tokens_total` is an optional cap on all the
    tokens a run spends across its turns; the runner raises `TokenLimitError` once a run passes it.
    `max_retries` is how many times the runner sends a request again after a 429, 529, 5xx or
    connection failure; it is not the validation retry, and 0 turns retries off.
    """

    max_tool_turns: int = Field(gt=0)
    timeout_s: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_tokens_total: int | None = Field(default=None, gt=0)
    max_retries: int = Field(default=2, ge=0)


class WeightsSpec(_Model):
    """The `scoring.weights` block: how much each of the judge's four scores counts, from 0 to 1."""

    correctness_risk: float = Field(ge=0, le=1)
    maintainability: float = Field(ge=0, le=1)
    security_risk: float = Field(ge=0, le=1)
    test_coverage: float = Field(ge=0, le=1)


class ScoringSpec(_Model):
    """The `scoring` block: score weights, which must sum to 1, and the score that ships a change.

    The runtime applies both in `runtime.scoring`; the agent never sees them.
    """

    weights: WeightsSpec
    ship_threshold: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ScoringSpec:
        total = sum(self.weights.model_dump().values())
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"weights sum to {total:g}, expected 1")
        return self


class AgentSpecBase(_Model):
    """Fields shared by every agent kind.

    `path` is the manifest file the spec was loaded from. It is set by the loader and is not a
    manifest key. `version` is a full `MAJOR.MINOR.PATCH`. `team` is an optional label, one of
    `TEAMS`, that groups agents for reports; the resolver, the runner and the publish gate never
    read it, and changing it bumps no version.
    """

    name: Name
    version: Version
    kind: str
    description: str = Field(min_length=1)
    evals: EvalsSpec
    team: Team | None = None
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
    `scoring` is optional and only an agent whose output carries scores, such as the judge, sets it.
    `guard` is optional and names the action-guard agent, as `name@^MAJOR.MINOR`, that must allow
    each of this agent's tool calls that is not read-only; see `runtime.guard`.
    `sandbox` says the agent runs where it may write files and run commands: only then does the
    tool registry hand out the sandbox-only tools (`fs.write_file`, `shell.run`). It takes one of
    three values. `false`, the default, is no sandbox. `true` is the offline profile: the agent
    works on the repository it is given, and its commands run with no network. `spike` is the
    networked profile of `tools.sandbox`: the agent gets a fresh empty directory instead of a
    repository, its commands may reach the network and install packages, and the web tools stay
    available to it. `sandbox_profile` names the profile.
    `files` is optional and only a sandboxed agent that writes to a repository sets it: the paths
    its writes may touch, which the pipeline running it enforces (`FilesSpec`). Absent, the
    manifest sets no policy and a pipeline that needs one refuses the agent.
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
    scoring: ScoringSpec | None = None
    guard: AgentRange | None = None
    sandbox: bool | Literal["spike"] = False
    files: FilesSpec | None = None

    _input_class: type[BaseModel] | None = PrivateAttr(default=None)
    _output_class: type[BaseModel] | None = PrivateAttr(default=None)

    @field_validator("system_prompt", "schemas")
    @classmethod
    def _inside_directory(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in PurePosixPath(value.as_posix()).parts:
            raise ValueError("must be a relative path inside the agent directory")
        return value

    @model_validator(mode="after")
    def _files_need_a_sandbox(self) -> LLMSpec:
        if self.files is not None and not self.sandbox:
            raise ValueError("files: only an agent with a sandbox writes files")
        return self

    @property
    def sandbox_profile(self) -> Literal["offline", "spike"] | None:
        """The sandbox profile: `offline` for `sandbox: true`, `spike`, or None for no sandbox."""
        if self.sandbox == "spike":
            return "spike"
        return "offline" if self.sandbox is True else None

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

    def with_web_domains(self, domains: Iterable[str]) -> LLMSpec:
        """Return this spec with `domains` in place of the manifest's `tools.web.allow_domains`.

        The list replaces the manifest's, it is not merged with it, and it is validated as the
        manifest's is: a host that is not bare and lowercase raises a Pydantic `ValidationError`.
        The copy keeps the loaded models. A spec whose manifest lists no domain has no web tools,
        so it comes back unchanged: the override never hands out web tools the manifest does not.
        `evals.run` applies it for a case's `citations_resolve.allow_domains`, and `factory run`
        for `--allow-domain`.
        """
        if not self.tools.web.allow_domains:
            return self
        web = WebSpec(allow_domains=tuple(domains))
        return self.model_copy(update={"tools": self.tools.model_copy(update={"web": web})})


KINDS = ("llm", "jev")
