"""The LLM agent runner: one agent run on the Anthropic SDK Tool Runner, with structured output.

`run` validates the payload against the agent's input model and sends it as the first user
message. The model may call the agent's scoped tools; each call goes through the run's hook (see
`runtime.hooks`) before and after it executes. The final answer arrives as JSON under the output
model's schema, through `output_config.format`, and is validated against the output model. A
failed validation is retried once with the errors; a second failure ends the run.

`build_request` is the only place a request is built, so the rules in `docs/CONVENTIONS.md`
(Request fields) hold in one place. Every way a run can fail raises a typed error from
`runtime.errors`, or `AuthOrConfigError` for the API key, and every run whose payload validated
writes exactly one trace row, on success or failure.
"""

import json
import os
import time
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anthropic
from anthropic.lib.tools import BetaBuiltinFunctionTool
from anthropic.lib.tools import ToolError as SDKToolError
from anthropic.types.beta import BetaToolParam, BetaUsage
from pydantic import BaseModel, ValidationError

from factory.exit import AuthOrConfigError, FactoryError, UsageError
from registry.spec import LLMSpec
from runtime.errors import (
    AgentTimeoutError,
    ModelAPIError,
    OutputTruncatedError,
    OutputValidationError,
    RefusalError,
    ToolCrashError,
    TurnLimitError,
)
from runtime.hooks import AllowAll, ToolCall, ToolHook, ToolResult
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace, TraceRow
from tools import MAX_OUTPUT_BYTES, TOOLS, ToolError, dotted_name, tool_defs
from tools._common import ToolUnavailableError

API_KEY_ENV = "ANTHROPIC_API_KEY"
"""The environment variable the default client reads the API key from."""

FORBIDDEN_REQUEST_KEYS = frozenset(
    {"temperature", "top_p", "top_k", "tool_choice", "max_iterations"}
)
"""Top-level keys `build_request` never sets. A thinking budget is never set either."""

_KEY_HINT = f"store it with `agb sys secrets set {API_KEY_ENV}` and export it"

_TRUNCATED = ("max_tokens", "model_context_window_exceeded")
_FINISHED = ("end_turn", "stop_sequence")


def build_request(
    spec: LLMSpec, messages: Sequence[Mapping[str, Any]], tools: Sequence[Any] = ()
) -> dict[str, Any]:
    """Return the keyword arguments for `client.beta.messages.tool_runner` for one agent.

    Sends the manifest's model, `max_tokens` from `limits.max_output_tokens`, the system prompt as
    one text block with `cache_control`, adaptive thinking, and `output_config` with the manifest's
    effort and the output model's JSON schema as the format. `timeout` is `limits.timeout_s`, per
    request. `thread=False` sends the full history every turn, so the runner holds no server state
    and the transcript alone can restart a conversation. Never sets a key in
    `FORBIDDEN_REQUEST_KEYS` or a thinking budget. `messages` and `tools` are copied, not kept.
    """
    system_prompt = spec.system_prompt_file.read_text(encoding="utf-8")
    return {
        "model": spec.model,
        "max_tokens": spec.limits.max_output_tokens,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": [dict(message) for message in messages],
        "tools": list(tools),
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": spec.effort,
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(spec.output_class),
            },
        },
        "thread": False,
        "timeout": spec.limits.timeout_s,
    }


def run(
    spec: LLMSpec,
    payload: Mapping[str, Any] | BaseModel,
    trace: Trace,
    *,
    repo_path: str | Path | None = None,
    pipeline_run_id: str | None = None,
    hook: ToolHook | None = None,
    client: anthropic.Anthropic | None = None,
    tag: str | None = None,
) -> BaseModel:
    """Run the LLM agent `spec` on `payload` and return an instance of its output model.

    `spec` is a resolved spec with its models attached (`registry.resolver.resolve`). `payload` is
    a dict or a model instance; it is validated against the input model, and a mismatch raises
    `UsageError` before anything is sent or traced. `repo_path` is the repository root the tools
    read; it is required when the agent allows any tool, and leaving it out is a `UsageError`
    that is not traced either. `hook` sees every tool call (default `AllowAll`); pass a new one
    per run. `client` defaults to one built from `ANTHROPIC_API_KEY`, and a missing key raises
    `AuthOrConfigError`. `pipeline_run_id` and `tag` (for example `eval:code-reviewer@1.2.0`) land
    in the trace row.

    Past those two checks, exactly one row is written to `trace`, whether the run succeeds or
    fails. Raises the `runtime.errors.AgentError` subclasses for failures of the run itself,
    `AuthOrConfigError` for a missing or rejected key, `ToolError` for a bad tool list, and any
    `FactoryError` a hook raises, unchanged.
    """
    payload_json = _validate_payload(spec, payload)
    if spec.tools.allow and repo_path is None:
        raise UsageError(
            f"{spec.address} allows tools, so it needs a repository root",
            hint="pass repo_path, the top of the git work tree the tools read",
        )
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    start = time.monotonic()
    state = _RunState(
        spec=spec,
        hook=hook if hook is not None else AllowAll(),
        repo_path=repo_path,
        deadline=start + spec.limits.timeout_s,
    )
    output: BaseModel | None = None
    error: BaseException | None = None
    try:
        output = _run(spec, payload_json, state, client)
        return output
    except BaseException as exc:
        error = exc
        raise
    finally:
        usage = state.usage
        trace.record(
            TraceRow(
                run_id=run_id,
                agent_ref=spec.address,
                kind="llm",
                started_at=started_at,
                duration_ms=int((time.monotonic() - start) * 1000),
                input_json=payload_json,
                pipeline_run_id=pipeline_run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_usd=cost_usd(spec.model, usage),
                output_json=None if output is None else output.model_dump(mode="json"),
                error=None if error is None else f"{type(error).__name__}: {error}",
                tag=tag,
            )
        )


class _RunStopped(Exception):
    """Raised inside a tool call once the run must end; the loop raises the stored cause."""


@dataclass
class _RunState:
    """What one run accumulates: transcript, usage, tool turns, and why it must stop, if it must."""

    spec: LLMSpec
    hook: ToolHook
    repo_path: str | Path | None
    deadline: float
    transcript: list[dict[str, Any]] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0, 0, 0))
    turns: int = 0
    abort: FactoryError | None = None
    crash: tuple[str, Exception] | None = None

    @property
    def stopping(self) -> bool:
        return self.abort is not None or self.crash is not None

    def add_usage(self, usage: BetaUsage) -> None:
        self.usage = TokenUsage(
            input_tokens=(self.usage.input_tokens or 0) + (usage.input_tokens or 0),
            output_tokens=(self.usage.output_tokens or 0) + (usage.output_tokens or 0),
            cache_read_tokens=(self.usage.cache_read_tokens or 0)
            + (usage.cache_read_input_tokens or 0),
            cache_write_tokens=(self.usage.cache_write_tokens or 0)
            + (usage.cache_creation_input_tokens or 0),
        )

    @contextmanager
    def guard(self, tool: str) -> Generator[None]:
        """Turn a hook's `FactoryError` into an abort and anything else into a crash of `tool`."""
        try:
            yield
        except FactoryError as exc:
            self.abort = exc
            raise _RunStopped from exc
        except Exception as exc:
            self.crash = (tool, exc)
            raise _RunStopped from exc

    def raise_if_stopping(self) -> None:
        if self.abort is not None:
            raise self.abort
        if self.crash is not None:
            tool, exc = self.crash
            raise ToolCrashError(
                f"tool {tool} crashed: {type(exc).__name__}: {exc}",
                tool=tool,
                agent=self.spec.address,
                transcript=self.transcript,
            ) from exc

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            timeout_s = self.spec.limits.timeout_s
            raise AgentTimeoutError(
                f"{self.spec.address} ran past limits.timeout_s ({timeout_s}s)",
                timeout_s=timeout_s,
                agent=self.spec.address,
                transcript=self.transcript,
            )


class _ScopedTool(BetaBuiltinFunctionTool):
    """One registry tool as the SDK runner sees it: hook, argument check, then the implementation.

    An error result is raised as the SDK's `ToolError`, which the runner sends back with
    `is_error: true`. A reason to end the run is stored on the run state and raised by the loop
    after the step, because the SDK runner turns every exception from `call` into a tool result.
    """

    def __init__(self, definition: dict[str, Any], state: _RunState) -> None:
        self._definition = definition
        self._dotted = dotted_name(definition["name"])
        self._impl = TOOLS[self._dotted][1]
        self._state = state

    def to_dict(self) -> BetaToolParam:
        return cast("BetaToolParam", self._definition)

    def call(self, input: object) -> str:
        try:
            result = self._call(input)
        except _RunStopped:
            raise SDKToolError("not run: the run is ending") from None
        if result.is_error:
            raise SDKToolError(result.content)
        return result.content

    def _call(self, input: object) -> ToolResult:
        state = self._state
        if state.stopping:
            raise _RunStopped
        if not isinstance(input, Mapping):
            return ToolResult("tool input must be a JSON object", is_error=True)
        arguments = {str(key): value for key, value in input.items()}
        call = ToolCall(name=self._dotted, arguments=arguments, agent=state.spec.address)
        with state.guard(self._dotted):
            decision = state.hook.before_tool(call)
        if not decision.allowed:
            return ToolResult(decision.message or f"{self._dotted} was denied", is_error=True)
        result = self._execute(arguments)
        with state.guard(self._dotted):
            state.hook.after_tool(call, result)
        return result

    def _execute(self, arguments: dict[str, Any]) -> ToolResult:
        problem = _argument_problem(self._definition["input_schema"], arguments)
        if problem is not None:
            return ToolResult(problem, is_error=True)
        state = self._state
        try:
            content = self._impl(state.repo_path, **arguments, max_bytes=MAX_OUTPUT_BYTES)
        except ToolUnavailableError as exc:
            state.abort = exc
            raise _RunStopped from exc
        except ToolError as exc:
            hint = "" if exc.hint is None else f" (hint: {exc.hint})"
            return ToolResult(f"{exc.message}{hint}", is_error=True)
        except Exception as exc:
            state.crash = (self._dotted, exc)
            raise _RunStopped from exc
        return ToolResult(content)


def _argument_problem(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    """Return why `arguments` do not fit a registry tool's `schema`, or None when they fit.

    Checks what the registry's schemas use: unknown names, which also keeps the model from setting
    `max_bytes`; missing required names; and string types.
    """
    properties: Mapping[str, Any] = schema.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        return f"unknown arguments: {', '.join(unknown)}; allowed: {', '.join(properties)}"
    missing = [name for name in schema.get("required", ()) if name not in arguments]
    if missing:
        return f"missing required arguments: {', '.join(missing)}"
    wrong = [
        name
        for name, value in arguments.items()
        if properties[name].get("type") == "string" and not isinstance(value, str)
    ]
    if wrong:
        return f"arguments must be strings: {', '.join(wrong)}"
    return None


def _validate_payload(spec: LLMSpec, payload: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """Return `payload` validated against the input model, as JSON-ready data.

    Raises `UsageError` naming each failing field. The error never repeats the payload's values.
    """
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    try:
        model = spec.input_class.model_validate(data)
    except ValidationError as exc:
        raise UsageError(
            f"payload does not match {spec.input_model} for {spec.address}: {_problems(exc)}",
            hint=f"see {spec.schemas} in the agent's directory",
        ) from exc
    return model.model_dump(mode="json")


def _problems(exc: ValidationError) -> str:
    """Return the field paths and messages of `exc`, joined by `; `, without the input values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors(include_url=False)
    )


def _run(
    spec: LLMSpec,
    payload_json: dict[str, Any],
    state: _RunState,
    client: anthropic.Anthropic | None,
) -> BaseModel:
    """Build the tools and client, converse, validate, and retry validation once."""
    tools = [
        _ScopedTool(definition, state)
        for definition in tool_defs(spec.tools.allow, spec.tools.deny)
    ]
    if client is None:
        if not os.environ.get(API_KEY_ENV):
            raise AuthOrConfigError(f"{API_KEY_ENV} is not set", hint=_KEY_HINT)
        with anthropic.Anthropic() as default_client:
            return _converse_and_validate(default_client, spec, payload_json, state, tools)
    return _converse_and_validate(client, spec, payload_json, state, tools)


def _converse_and_validate(
    client: anthropic.Anthropic,
    spec: LLMSpec,
    payload_json: dict[str, Any],
    state: _RunState,
    tools: list[_ScopedTool],
) -> BaseModel:
    state.transcript.append({"role": "user", "content": json.dumps(payload_json)})
    text = _converse(client, spec, state, tools)
    try:
        return spec.output_class.model_validate_json(text)
    except ValidationError as exc:
        state.transcript.append(
            {
                "role": "user",
                "content": (
                    f"Your answer does not validate against {spec.output_model}: {_problems(exc)}. "
                    "Reply again with only the corrected JSON object."
                ),
            }
        )
    state.check_deadline()
    text = _converse(client, spec, state, tools)
    try:
        return spec.output_class.model_validate_json(text)
    except ValidationError as exc:
        raise OutputValidationError(
            f"{spec.address} answered with invalid {spec.output_model} twice: {_problems(exc)}",
            agent=spec.address,
            transcript=state.transcript,
        ) from exc


def _converse(
    client: anthropic.Anthropic, spec: LLMSpec, state: _RunState, tools: list[_ScopedTool]
) -> str:
    """Run the tool loop from `state.transcript` to a final answer and return the answer's text.

    Appends every assistant message and tool result to the transcript as it goes, and maps the
    stop reasons and API errors to typed errors.
    """
    runner = client.beta.messages.tool_runner(**build_request(spec, state.transcript, tools))
    try:
        for message in runner:
            state.add_usage(message.usage)
            state.transcript.append(
                {"role": "assistant", "content": [_block(block) for block in message.content]}
            )
            reason = message.stop_reason
            if reason == "refusal":
                details = message.stop_details
                category = None if details is None else details.category
                raise RefusalError(
                    f"{spec.address} refused (category: {category})",
                    category=category,
                    agent=spec.address,
                    transcript=state.transcript,
                )
            if reason in _TRUNCATED:
                raise OutputTruncatedError(
                    f"{spec.address} stopped at {reason} "
                    f"(limits.max_output_tokens is {spec.limits.max_output_tokens})",
                    stop_reason=reason,
                    agent=spec.address,
                    transcript=state.transcript,
                )
            if reason in _FINISHED:
                return "".join(block.text for block in message.content if block.type == "text")
            if reason == "tool_use":
                state.turns += 1
                if state.turns > spec.limits.max_tool_turns:
                    raise TurnLimitError(
                        f"{spec.address} asked for more than "
                        f"limits.max_tool_turns ({spec.limits.max_tool_turns}) tool turns",
                        limit=spec.limits.max_tool_turns,
                        agent=spec.address,
                        transcript=state.transcript,
                    )
                response = runner.generate_tool_call_response()
                if response is not None:
                    state.transcript.append(json.loads(json.dumps(response)))
                state.raise_if_stopping()
            elif reason != "pause_turn":
                raise ModelAPIError(
                    f"{spec.address} stopped with an unexpected stop_reason {reason!r}",
                    agent=spec.address,
                    transcript=state.transcript,
                )
            state.check_deadline()
    except anthropic.APITimeoutError as exc:
        raise AgentTimeoutError(
            f"a Messages API request for {spec.address} timed out "
            f"(limits.timeout_s is {spec.limits.timeout_s}s)",
            timeout_s=spec.limits.timeout_s,
            agent=spec.address,
            transcript=state.transcript,
        ) from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AuthOrConfigError(
            f"the Messages API refused the key in {API_KEY_ENV} ({exc.status_code})",
            hint=_KEY_HINT,
        ) from exc
    except anthropic.APIError as exc:
        raise ModelAPIError(
            f"the Messages API failed for {spec.address}: {type(exc).__name__}: {exc.message}",
            agent=spec.address,
            transcript=state.transcript,
        ) from exc
    finally:
        runner.close()
    raise ModelAPIError(
        f"{spec.address} ended without a final answer",
        agent=spec.address,
        transcript=state.transcript,
    )


def _block(block: Any) -> dict[str, Any]:
    """Return a content block as the request param it came from; thinking keeps its signature."""
    data: dict[str, Any] = block.model_dump(mode="json", exclude_none=True)
    data.pop("parsed_output", None)
    return data
