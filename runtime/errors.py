"""The typed errors an LLM agent run raises.

Every failure of a run that is not a configuration or tool-list problem is an `AgentError`. It
carries the agent's address and the transcript of the run so far, as plain message dicts in the
shape the Messages API takes, so a caller can log or replay what the model saw and said. Thinking
blocks in the transcript keep their signatures.

A missing or rejected API key is not an `AgentError`: it raises `factory.exit.AuthOrConfigError`
(exit 4), with a hint that names the variable and never its value.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from factory.exit import FactoryError

type Transcript = tuple[dict[str, Any], ...]


class AgentError(FactoryError):
    """An agent run failed (exit 1). `agent` is the `name@version` address of the agent."""

    def __init__(
        self,
        message: str,
        *,
        agent: str,
        transcript: Iterable[Mapping[str, Any]] = (),
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint)
        self.agent = agent
        self.transcript: Transcript = tuple(dict(item) for item in transcript)


class OutputValidationError(AgentError):
    """The final answer failed output-model validation twice: once, then once more after retry."""


class RefusalError(AgentError):
    """The model refused. `category` is the API's `stop_details.category`, or None when absent."""

    def __init__(self, message: str, *, category: str | None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.category = category


class OutputTruncatedError(AgentError):
    """The reply hit `max_tokens` or the context window. `stop_reason` says which."""

    def __init__(self, message: str, *, stop_reason: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.stop_reason = stop_reason


class TurnLimitError(AgentError):
    """The model asked for more tool turns than the manifest's `limits.max_tool_turns`."""

    def __init__(self, message: str, *, limit: int, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.limit = limit


class AgentTimeoutError(AgentError):
    """The run went past the manifest's `limits.timeout_s`, in one request or across all of them."""

    def __init__(self, message: str, *, timeout_s: int, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.timeout_s = timeout_s


class ToolCrashError(AgentError):
    """A tool implementation or a hook raised something other than a `ToolError`.

    `tool` is the dotted tool name. The original exception is the `__cause__`. A crash is a bug in
    the factory, not something the model can work around, so the run stops instead of feeding the
    traceback back to the model.
    """

    def __init__(self, message: str, *, tool: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.tool = tool


class ModelAPIError(AgentError):
    """The Messages API returned an error other than an authentication or permission failure."""


class TokenLimitError(AgentError):
    """The run used more tokens than the manifest's `limits.max_tokens_total`.

    `limit` is the cap and `used` the tokens counted when the run stopped. The transcript is the
    partial one, up to and including the turn that passed the cap.
    """

    def __init__(self, message: str, *, limit: int, used: int, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.limit = limit
        self.used = used
