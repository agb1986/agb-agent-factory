"""The hook protocol the runner calls around every tool execution.

For each tool call the model makes, the runner builds a `ToolCall` and asks the hook
`before_tool(call)`. An allowed call runs, and the hook then sees `after_tool(call, result)`. A
denied call never runs: the decision's message goes back to the model as an error tool result, and
`after_tool` is not called. The default hook, `AllowAll`, allows everything and records nothing.

A hook holds whatever state it needs for one run, such as a denial count. The runner never ends a
run because of a denial; a hook that wants that (the action guard in #23) raises a `FactoryError`
from `before_tool`, which the runner lets through unchanged. Any other exception from a hook ends
the run with `runtime.errors.ToolCrashError`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, Self


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call the model asked for.

    `name` is the dotted tool name (`git.read_file`), `arguments` the model's input as given, and
    `agent` the `name@version` address of the agent that made the call.
    """

    name: str
    arguments: Mapping[str, Any]
    agent: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ToolDecision:
    """A hook's answer to `before_tool`. Build one with `allow()` or `deny(message)`."""

    allowed: bool
    message: str | None = None

    @classmethod
    def allow(cls) -> Self:
        """Run the call."""
        return cls(allowed=True)

    @classmethod
    def deny(cls, message: str) -> Self:
        """Skip the call and send `message` to the model as an error tool result."""
        return cls(allowed=False, message=message)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What an allowed call sent back to the model. `is_error` is true when the tool failed."""

    content: str
    is_error: bool = False


class ToolHook(Protocol):
    """The two calls the runner makes around every tool execution. See the module docstring."""

    def before_tool(self, call: ToolCall) -> ToolDecision: ...

    def after_tool(self, call: ToolCall, result: ToolResult) -> None: ...


class AllowAll:
    """The default hook: every call runs and nothing is recorded."""

    def before_tool(self, call: ToolCall) -> ToolDecision:
        return ToolDecision.allow()

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        return None
