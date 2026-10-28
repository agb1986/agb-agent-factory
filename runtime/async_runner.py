"""The async LLM agent runner: `arun`, one agent run on the SDK's async Tool Runner.

`arun` keeps the contract of `runtime.runner.run`: payload validation, the scoped tools and their
hooks, the one validation retry, the typed errors, the limits, the budget, and exactly one trace
row per run. This module holds the one conversation loop, and `run` drives it on an event loop of
its own. The request, the run state, the scoped tools and the retry policy stay in
`runtime.runner`, so nothing is forked between the two.

Concurrency. With an `anthropic.AsyncAnthropic` client, the default here, requests are awaited on
the loop. Tool calls and hooks are blocking code (file reads, `rg`, `git`, the action guard's own
agent run), so each runs on a worker thread through `asyncio.to_thread`, one call at a time per
run, as before. With a synchronous `anthropic.Anthropic` client, the default of `run`, each step of
the SDK's synchronous Tool Runner runs on a worker thread instead. The trace row is written on the
loop; `Trace` serialises its writes with a lock, so runs gathered on one loop and hooks on worker
threads can share one `Trace` (see `runtime.trace`).

Nested runs. The action guard runs its agent through `runtime.runner.run` from inside a hook, on a
worker thread. An `AsyncAnthropic` client belongs to the loop it is used on, so when the outer run
passes its async client down, `run` hands the guard's run to the outer loop (`_LOOP`) and blocks
its worker thread until that run ends. The handed-over run needs a worker thread only for a tool
call or a non-default `sleep`, and guard agents have no tools, so it cannot starve the loop's
default executor of the threads it waits on.
"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import anthropic
from anthropic.lib.tools import BetaAsyncBuiltinFunctionTool, BetaAsyncToolRunner, BetaToolRunner
from anthropic.types.beta import BetaToolParam, BetaUserMessageParam
from anthropic.types.beta.parsed_beta_message import ParsedBetaMessage
from pydantic import BaseModel, ValidationError

from factory.exit import AuthOrConfigError, UsageError
from registry.spec import LLMSpec
from runtime.budget import Budget
from runtime.errors import (
    AgentTimeoutError,
    ModelAPIError,
    OutputTruncatedError,
    OutputValidationError,
    RefusalError,
    TurnLimitError,
)
from runtime.guard import build_guard_hook
from runtime.hooks import AllowAll, ToolHook
from runtime.pricing import cost_usd
from runtime.runner import (
    _FINISHED,
    _KEY_HINT,
    _TRUNCATED,
    API_KEY_ENV,
    _api_error,
    _block,
    _delay,
    _problems,
    _Retry,
    _retryable,
    _RunState,
    _ScopedTool,
    _validate_payload,
    build_request,
)
from runtime.trace import Trace, TraceRow
from tools import tool_defs

_LOOP: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar("_LOOP", default=None)
"""The loop of the run this context is inside, so a nested `run` can hand an async run to it."""


async def arun(
    spec: LLMSpec,
    payload: Mapping[str, Any] | BaseModel,
    trace: Trace,
    *,
    repo_path: str | Path | None = None,
    pipeline_run_id: str | None = None,
    parent_run_id: str | None = None,
    run_id: str | None = None,
    hook: ToolHook | None = None,
    client: anthropic.AsyncAnthropic | anthropic.Anthropic | None = None,
    tag: str | None = None,
    budget: Budget | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BaseModel:
    """Run the LLM agent `spec` on `payload` without blocking the loop; return its output model.

    The contract of `runtime.runner.run` holds: the same arguments, checks, errors, limits and
    budget charges, and exactly one trace row once the payload and `repo_path` pass. The
    differences: `client` defaults to an `anthropic.AsyncAnthropic` built from `ANTHROPIC_API_KEY`
    with `max_retries=0`, and a synchronous client passed in is stepped on worker threads; `sleep`
    is awaited between resends; tools and hooks run on worker threads, so a hook must not touch the
    loop. Runs gathered on one loop may share a `Budget` and a `Trace`.
    """
    return await _arun(
        spec,
        payload,
        trace,
        repo_path=repo_path,
        pipeline_run_id=pipeline_run_id,
        parent_run_id=parent_run_id,
        run_id=run_id,
        hook=hook,
        client=client,
        tag=tag,
        budget=budget,
        sleep=sleep,
        sync_default=False,
    )


async def _arun(
    spec: LLMSpec,
    payload: Mapping[str, Any] | BaseModel,
    trace: Trace,
    *,
    repo_path: str | Path | None,
    pipeline_run_id: str | None,
    parent_run_id: str | None,
    run_id: str | None,
    hook: ToolHook | None,
    client: anthropic.AsyncAnthropic | anthropic.Anthropic | None,
    tag: str | None,
    budget: Budget | None,
    sleep: Callable[[float], Awaitable[None]],
    sync_default: bool,
) -> BaseModel:
    """`arun`, building a synchronous default client when `sync_default` is true, as `run` does."""
    payload_json = _validate_payload(spec, payload)
    if spec.tools.allow and repo_path is None:
        raise UsageError(
            f"{spec.address} allows tools, so it needs a repository root",
            hint="pass repo_path, the top of the git work tree the tools read",
        )
    if budget is not None:
        budget.check()
    run_id = run_id or str(uuid.uuid4())
    if spec.guard is not None:
        hook = build_guard_hook(
            spec,
            trace,
            parent_run_id=run_id,
            repo_path=repo_path,
            pipeline_run_id=pipeline_run_id,
            tag=tag,
            inner=hook,
            client=client,
            budget=budget,
        )
    started_at = datetime.now(UTC)
    start = time.monotonic()
    state = _RunState(
        spec=spec,
        hook=hook if hook is not None else AllowAll(),
        repo_path=repo_path,
        deadline=start + spec.limits.timeout_s,
        budget=budget,
        sleep=sleep,
    )
    token = _LOOP.set(asyncio.get_running_loop())
    output: BaseModel | None = None
    error: BaseException | None = None
    try:
        output = await _run(spec, payload_json, state, client, sync_default=sync_default)
        return output
    except BaseException as exc:
        error = exc
        raise
    finally:
        _LOOP.reset(token)
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
                parent_run_id=parent_run_id,
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
        # A failed run only adds its cost, so the budget never hides the error that ended it.
        state.charge_budget(strict=error is None)


class _AsyncScopedTool(BetaAsyncBuiltinFunctionTool):
    """A `runtime.runner._ScopedTool` for the async Tool Runner: the same call, on a worker thread."""

    def __init__(self, tool: _ScopedTool) -> None:
        self._tool = tool

    def to_dict(self) -> BetaToolParam:
        return self._tool.to_dict()

    async def call(self, input: object) -> str:
        return await asyncio.to_thread(self._tool.call, input)


class _SyncSteps:
    """The SDK's synchronous Tool Runner behind the async runner's face, one step per thread hop."""

    def __init__(self, runner: BetaToolRunner[Any]) -> None:
        self._runner = runner

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> ParsedBetaMessage[Any]:
        message = await asyncio.to_thread(self._step)
        if message is None:
            raise StopAsyncIteration
        return message

    def _step(self) -> ParsedBetaMessage[Any] | None:
        return next(self._runner, None)

    async def generate_tool_call_response(self) -> BetaUserMessageParam | None:
        return await asyncio.to_thread(self._runner.generate_tool_call_response)

    async def close(self) -> None:
        self._runner.close()


async def _run(
    spec: LLMSpec,
    payload_json: dict[str, Any],
    state: _RunState,
    client: anthropic.AsyncAnthropic | anthropic.Anthropic | None,
    *,
    sync_default: bool,
) -> BaseModel:
    """Build the tools and client, converse, validate, and retry validation once."""
    tools = [
        _ScopedTool(definition, state)
        for definition in tool_defs(spec.tools.allow, spec.tools.deny, sandbox=spec.sandbox)
    ]
    if client is not None:
        return await _converse_and_validate(client, spec, payload_json, state, tools)
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(f"{API_KEY_ENV} is not set", hint=_KEY_HINT)
    if sync_default:
        with anthropic.Anthropic(max_retries=0) as sync_client:
            return await _converse_and_validate(sync_client, spec, payload_json, state, tools)
    async with anthropic.AsyncAnthropic(max_retries=0) as async_client:
        return await _converse_and_validate(async_client, spec, payload_json, state, tools)


async def _converse_and_validate(
    client: anthropic.AsyncAnthropic | anthropic.Anthropic,
    spec: LLMSpec,
    payload_json: dict[str, Any],
    state: _RunState,
    tools: list[_ScopedTool],
) -> BaseModel:
    state.transcript.append({"role": "user", "content": json.dumps(payload_json)})
    text = await _converse(client, spec, state, tools)
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
    text = await _converse(client, spec, state, tools)
    try:
        return spec.output_class.model_validate_json(text)
    except ValidationError as exc:
        raise OutputValidationError(
            f"{spec.address} answered with invalid {spec.output_model} twice: {_problems(exc)}",
            agent=spec.address,
            transcript=state.transcript,
        ) from exc


async def _converse(
    client: anthropic.AsyncAnthropic | anthropic.Anthropic,
    spec: LLMSpec,
    state: _RunState,
    tools: list[_ScopedTool],
) -> str:
    """Run the tool loop to a final answer, sending a failed request again within the limits.

    A retryable failure (see `runtime.runner._retryable`) rebuilds the request from
    `state.transcript`, which is consistent whenever a request fails, and sends it again after a
    backoff. It gives up with a `ModelAPIError` after `limits.max_retries` resends, or when the
    wait would pass the deadline.
    """
    failures = 0
    while True:
        before = state.messages
        try:
            return await _converse_once(client, spec, state, tools)
        except _Retry as retry:
            failures = 1 if state.messages > before else failures + 1
            retries = failures - 1
            if retries >= spec.limits.max_retries:
                raise _api_error(spec, state, retry.cause, retries=retries) from retry.cause
            delay = _delay(failures, retry.cause)
            if time.monotonic() + delay >= state.deadline:
                raise ModelAPIError(
                    f"the Messages API failed for {spec.address}: not retrying, a {delay:g}s wait "
                    f"would pass limits.timeout_s ({spec.limits.timeout_s}s): "
                    f"{type(retry.cause).__name__}: {retry.cause.message}",
                    agent=spec.address,
                    transcript=state.transcript,
                ) from retry.cause
            await state.sleep(delay)


def _open(
    client: anthropic.AsyncAnthropic | anthropic.Anthropic,
    spec: LLMSpec,
    state: _RunState,
    tools: list[_ScopedTool],
) -> BetaAsyncToolRunner[Any] | _SyncSteps:
    """Start a Tool Runner for the request `state.transcript` makes, async whatever the client."""
    if isinstance(client, anthropic.AsyncAnthropic):
        async_tools = [_AsyncScopedTool(tool) for tool in tools]
        return client.beta.messages.tool_runner(
            **build_request(spec, state.transcript, async_tools)
        )
    return _SyncSteps(
        client.beta.messages.tool_runner(**build_request(spec, state.transcript, tools))
    )


async def _converse_once(
    client: anthropic.AsyncAnthropic | anthropic.Anthropic,
    spec: LLMSpec,
    state: _RunState,
    tools: list[_ScopedTool],
) -> str:
    """Run the tool loop from `state.transcript` to a final answer and return the answer's text.

    Appends every assistant message and tool result to the transcript as it goes, and maps the
    stop reasons and API errors to typed errors. A failure worth retrying raises `_Retry`.
    """
    steps = _open(client, spec, state, tools)
    try:
        while (message := await anext(steps, None)) is not None:
            state.add_usage(message.usage)
            state.messages += 1
            state.transcript.append(
                {"role": "assistant", "content": [_block(block) for block in message.content]}
            )
            state.check_limits()
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
                response = await steps.generate_tool_call_response()
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
        if _retryable(exc):
            raise _Retry(exc) from exc
        raise _api_error(spec, state, exc) from exc
    finally:
        await steps.close()
    raise ModelAPIError(
        f"{spec.address} ended without a final answer",
        agent=spec.address,
        transcript=state.transcript,
    )
