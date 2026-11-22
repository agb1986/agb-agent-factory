"""Models shared by the decomposition agents: what code-archaeology returns.

The archaeology of a codebase is the output of `code-archaeology` and part of the input of the
agent that plans an extraction from it. An agent's `schemas.py` is imported by file path and cannot
import a sibling agent's module, so the models live here and each `schemas.py` re-exports the ones
its manifest or its own models use.

The output model is sent to the Messages API as a JSON schema (`output_config.format`), so every
field is a plain type, a `Literal` or a nested model, and none has a default. Every list and every
string has a maximum length, so that an answer about one entry point fits the agent's
`max_output_tokens`. The API cannot enforce those lengths; Pydantic checks them when the reply is
parsed, and a failure costs the run its one validation retry.

A symbol is written the way `deps.call_graph` and `deps.dead_code` write it: the path of a Python
file relative to the repository root, then `::` and the qualified name. A path alone is the whole
module, or its top-level code. Dead code and calls are read for Python only (ADR 0005).
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# The shape `fixtures/ground_truth.py` uses for its symbols.
SYMBOL_PATTERN = r"^[^/:\s\\][^:\s\\]*\.py(::[A-Za-z_]\w*(\.[A-Za-z_]\w*)*)?$"
PATH_PATTERN = r"^[^/\s\\][^\s\\]*$"

Symbol = Annotated[str, StringConstraints(max_length=160, pattern=SYMBOL_PATTERN)]
RepoPath = Annotated[str, StringConstraints(max_length=120, pattern=PATH_PATTERN)]

DeadCodeKind = Literal["function", "class", "method", "property", "module"]
DeadCodeRule = Literal["vulture", "private", "module", "through_dead"]
UnresolvedReason = Literal[
    "getattr",
    "container",
    "parameter",
    "unknown_type",
    "call_result",
    "variable",
    "ambiguous",
    "inherited",
    "import",
    "star_import",
    "not_defined",
    "expression",
]

_STRICT = ConfigDict(extra="forbid")


class ModuleEntry(BaseModel):
    """One module of the map: what it is for and which modules of the repository it imports."""

    model_config = _STRICT

    path: RepoPath = Field(description="Path of the module, relative to the repository root.")
    role: str = Field(
        min_length=1, max_length=160, description="One line: what the module is responsible for."
    )
    imports: list[RepoPath] = Field(
        max_length=20,
        description="Paths of the modules of the repository it imports, from deps.graph.",
    )


class DataFlowStep(BaseModel):
    """One function that reads, changes, stores or passes on the data."""

    model_config = _STRICT

    symbol: Symbol = Field(description="The function, as path::Qualified.name.")
    does: str = Field(
        min_length=1, max_length=160, description="One line: what it does with the data."
    )


class DataFlow(BaseModel):
    """How one piece of data travels between modules, step by step in order."""

    model_config = _STRICT

    data: str = Field(min_length=1, max_length=160, description="One line naming the data.")
    steps: list[DataFlowStep] = Field(
        min_length=2,
        max_length=12,
        description="The functions the data goes through, in order, at least two.",
    )


class DeadCodeEntry(BaseModel):
    """One symbol nothing uses, with the evidence of `deps.dead_code` for it."""

    model_config = _STRICT

    symbol: Symbol = Field(
        description="The symbol as the tool writes it; a path alone is a whole module."
    )
    kind: DeadCodeKind = Field(description="The kind the tool gives.")
    line: int = Field(ge=1, description="The line the tool gives.")
    rule: DeadCodeRule = Field(description="The rule the tool gives for the entry.")
    confidence: int = Field(ge=0, le=100, description="The confidence the tool gives.")
    evidence: str = Field(
        min_length=1,
        max_length=240,
        description="The tool's detail, and what reading the source added, in one line.",
    )


class DynamicLookup(BaseModel):
    """One symbol that looks unused but that a lookup by a computed name may reach. Not dead."""

    model_config = _STRICT

    symbol: Symbol = Field(description="The symbol as the tool writes it.")
    kind: DeadCodeKind = Field(description="The kind the tool gives.")
    line: int = Field(ge=1, description="The line the tool gives.")
    rule: DeadCodeRule = Field(description="The rule the tool gives for the entry.")
    confidence: int = Field(ge=0, le=100, description="The confidence the tool gives.")
    lookup_site: Symbol = Field(description="The symbol whose code does the lookup.")
    lookup: str = Field(
        min_length=1, max_length=160, description="The lookup expression, copied from the tool."
    )


class PathCall(BaseModel):
    """One call of an execution path: an edge of the call graph, or a call it could not resolve."""

    model_config = _STRICT

    status: Literal["resolved", "unresolved"] = Field(
        description="resolved when deps.call_graph lists the edge, unresolved when it lists the "
        "call as unresolved."
    )
    caller: Symbol = Field(description="The symbol whose code makes the call.")
    callee: Symbol | None = Field(
        description="The symbol called; null when the call is unresolved, which has no known one."
    )
    line: int = Field(ge=1, description="The line of the call in the caller's file.")
    expression: str | None = Field(
        max_length=200,
        description="The called expression copied from the tool; null when the call is resolved.",
    )
    reason: UnresolvedReason | None = Field(
        description="The tool's reason; null when the call is resolved."
    )

    @model_validator(mode="after")
    def _status_matches_the_fields(self) -> Self:
        if self.status == "resolved":
            if self.callee is None or self.expression is not None or self.reason is not None:
                raise ValueError("a resolved call has a callee, and null expression and reason")
        elif self.callee is not None or not self.expression or self.reason is None:
            raise ValueError("an unresolved call has an expression, a reason and a null callee")
        return self


class ExecutionPath(BaseModel):
    """One path of calls from the entry point, in order, with the calls it could not follow."""

    model_config = _STRICT

    title: str = Field(min_length=1, max_length=120, description="One line: what this path does.")
    calls: list[PathCall] = Field(
        min_length=1, max_length=14, description="The calls in order, starting at the entry point."
    )


class CodeArchaeologyOutput(BaseModel):
    """What was found in a codebase, read from one entry point."""

    model_config = _STRICT

    summary: str = Field(
        min_length=1,
        max_length=1500,
        description="A short account of the codebase from the entry point, and what is unknown.",
    )
    modules: list[ModuleEntry] = Field(
        max_length=40, description="The module map, the modules nearest the entry point first."
    )
    data_flows: list[DataFlow] = Field(
        max_length=10, description="The data flows between modules the entry point starts or joins."
    )
    dead_code: list[DeadCodeEntry] = Field(
        max_length=30, description="Symbols nothing uses, each with the tool's evidence."
    )
    dynamic_lookups: list[DynamicLookup] = Field(
        max_length=20, description="Symbols a dynamic lookup may reach, which are not dead."
    )
    execution_paths: list[ExecutionPath] = Field(
        max_length=14, description="The paths of calls that start at the entry point."
    )
