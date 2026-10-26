"""Tests for runtime/async_runner.py, against a mocked async Messages API, and the shared trace."""

import asyncio
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from factory.exit import AuthOrConfigError, UsageError
from registry.resolver import load_spec
from registry.spec import LLMSpec
from runtime import async_runner, runner
from runtime.async_runner import arun
from runtime.budget import Budget, BudgetExceeded
from runtime.errors import AgentTimeoutError
from runtime.hooks import ToolCall, ToolDecision, ToolResult
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace, TraceRow
from tests.runtime.test_runner import (
    MANIFEST,
    MODEL,
    SCHEMAS,
    SYSTEM_PROMPT,
    FakeAPI,
    RecordingHook,
    answer,
    asks_for_tool,
    message,
    replace_read_file,
    rows,
    thinking,
    tool_results,
)
from tests.runtime.test_runner_limits import PRICED_INPUT, api_error, cost_of

PAYLOAD = {"diff": "+ x"}


class FakeAsyncAPI(FakeAPI):
    """`FakeAPI` behind an `anthropic.AsyncAnthropic`. With `gate`, the first request waits on it."""

    def __init__(
        self,
        *replies: dict[str, Any] | httpx2.Response | Exception,
        gate: asyncio.Barrier | None = None,
    ) -> None:
        super().__init__(*replies)
        self.gate = gate

    async def handle_async(self, request: httpx2.Request) -> httpx2.Response:
        if self.gate is not None:
            gate, self.gate = self.gate, None
            await gate.wait()
        return self.handle(request)

    def async_client(self) -> anthropic.AsyncAnthropic:
        transport = httpx2.MockTransport(self.handle_async)
        return anthropic.AsyncAnthropic(
            api_key="test-key",
            http_client=anthropic.DefaultAsyncHttpxClient(transport=transport),
            max_retries=0,
        )


class AsyncSleeps:
    """A stand-in for `asyncio.sleep` that records the waits."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def usage_of(input_tokens: int = 10) -> TokenUsage:
    """The usage of one canned reply with `input_tokens`; see `message` in test_runner.py."""
    return TokenUsage(input_tokens, 5, 0, 0)


def extra_row(agent_ref: str) -> TraceRow:
    return TraceRow(
        run_id=str(uuid.uuid4()),
        agent_ref=agent_ref,
        kind="llm",
        started_at=datetime.now(UTC),
        duration_ms=0,
        input_json={},
    )


# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def make_spec(tmp_path: Path) -> Callable[..., LLMSpec]:
    """Write the test agent and load it; `allow` fills the manifest's tool list."""

    def make(*, allow: str = "git.read_file") -> LLMSpec:
        directory = tmp_path / "agents" / "reviewer"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "agent.yaml").write_text(
            MANIFEST.format(allow=allow, max_tool_turns=4, timeout_s=60, model=MODEL)
        )
        (directory / "system.md").write_text(SYSTEM_PROMPT)
        (directory / "schemas.py").write_text(SCHEMAS)
        spec = load_spec(directory / "agent.yaml")
        assert isinstance(spec, LLMSpec)
        return spec

    return make


@pytest.fixture
def spec(make_spec: Callable[..., LLMSpec]) -> LLMSpec:
    return make_spec()


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "trace.duckdb") as t:
        yield t


@pytest.fixture
def read_file(monkeypatch: pytest.MonkeyPatch) -> None:
    replace_read_file(
        monkeypatch, lambda repo_path, *, max_bytes, **arguments: f"contents of {arguments['path']}"
    )


# --- one run -----------------------------------------------------------------------------------


def test_happy_path_returns_the_output_model_and_writes_one_row(
    spec: LLMSpec, trace: Trace
) -> None:
    api = FakeAsyncAPI(answer())
    output = asyncio.run(
        arun(
            spec,
            PAYLOAD,
            trace,
            repo_path="/repo",
            pipeline_run_id="pipe-1",
            client=api.async_client(),
        )
    )
    assert output.model_dump() == {"ok": True}
    assert api.bodies[0]["messages"] == [{"role": "user", "content": '{"diff": "+ x"}'}]
    [row] = rows(trace)
    assert row["pipeline_run_id"] == "pipe-1"
    assert json.loads(row["output_json"]) == {"ok": True}
    assert row["error"] is None


def test_tool_call_then_output(spec: LLMSpec, trace: Trace, read_file: None) -> None:
    api = FakeAsyncAPI(asks_for_tool(), answer())
    output = asyncio.run(arun(spec, PAYLOAD, trace, repo_path="/repo", client=api.async_client()))
    assert output.model_dump() == {"ok": True}
    [result] = tool_results(api.bodies[1])
    assert (result["tool_use_id"], result["content"]) == ("tu_1", "contents of a.txt")
    assert len(rows(trace)) == 1


def test_hooks_fire_on_the_async_path(spec: LLMSpec, trace: Trace, read_file: None) -> None:
    hook = RecordingHook()
    api = FakeAsyncAPI(asks_for_tool(), answer())
    asyncio.run(arun(spec, PAYLOAD, trace, repo_path="/repo", hook=hook, client=api.async_client()))
    [call] = hook.before
    assert (call.name, dict(call.arguments)) == ("git.read_file", {"path": "a.txt", "ref": "HEAD"})
    assert hook.after == [(call, ToolResult("contents of a.txt"))]


def test_a_synchronous_client_is_stepped_on_worker_threads(
    spec: LLMSpec, trace: Trace, read_file: None
) -> None:
    api = FakeAPI(asks_for_tool(), answer())
    output = asyncio.run(arun(spec, PAYLOAD, trace, repo_path="/repo", client=api.client()))
    assert output.model_dump() == {"ok": True}
    [result] = tool_results(api.bodies[1])
    assert result["content"] == "contents of a.txt"


def test_a_retryable_failure_is_sent_again_after_an_awaited_sleep(
    spec: LLMSpec, trace: Trace
) -> None:
    api = FakeAsyncAPI(api_error(429), answer())
    sleeps = AsyncSleeps()
    asyncio.run(
        arun(spec, PAYLOAD, trace, repo_path="/repo", client=api.async_client(), sleep=sleeps)
    )
    assert len(api.bodies) == 2
    assert sleeps.waits == [0.5]


def test_request_timeout_is_an_agent_timeout(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAsyncAPI(httpx2.ReadTimeout("slow"))
    with pytest.raises(AgentTimeoutError):
        asyncio.run(arun(spec, PAYLOAD, trace, repo_path="/repo", client=api.async_client()))
    assert rows(trace)[0]["error"].startswith("AgentTimeoutError: ")


# --- the default client ------------------------------------------------------------------------


def test_default_client_is_an_async_client_without_sdk_retries(
    spec: LLMSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Built(Exception):
        pass

    seen: list[dict[str, Any]] = []

    def fake_client(**kwargs: Any) -> anthropic.AsyncAnthropic:
        seen.append(kwargs)
        raise Built

    monkeypatch.setenv(async_runner.API_KEY_ENV, "test-key")
    monkeypatch.setattr(async_runner.anthropic, "AsyncAnthropic", fake_client)
    with pytest.raises(Built):
        asyncio.run(arun(spec, PAYLOAD, trace, repo_path="/repo"))
    assert seen == [{"max_retries": 0}]


def test_missing_key_is_an_auth_error_naming_the_variable(
    spec: LLMSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(async_runner.API_KEY_ENV, raising=False)
    with pytest.raises(AuthOrConfigError, match=async_runner.API_KEY_ENV):
        asyncio.run(arun(spec, PAYLOAD, trace, repo_path="/repo"))


# --- run, the blocking face --------------------------------------------------------------------


def test_run_inside_a_running_loop_is_a_usage_error(spec: LLMSpec, trace: Trace) -> None:
    async def inside() -> None:
        runner.run(spec, PAYLOAD, trace, repo_path="/repo", client=FakeAPI(answer()).client())

    with pytest.raises(UsageError, match="running event loop"):
        asyncio.run(inside())
    assert rows(trace) == []


def test_run_accepts_an_async_client(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAsyncAPI(answer())
    output = runner.run(spec, PAYLOAD, trace, repo_path="/repo", client=api.async_client())
    assert output.model_dump() == {"ok": True}
    assert len(rows(trace)) == 1


def test_a_nested_run_from_a_hook_is_handed_to_the_outer_loop(
    make_spec: Callable[..., LLMSpec], trace: Trace, read_file: None
) -> None:
    inner_spec = make_spec(allow="")
    spec = make_spec()
    api = FakeAsyncAPI(asks_for_tool(), answer(), answer())
    client = api.async_client()
    threads: list[threading.Thread] = []

    class Nested(RecordingHook):
        """Runs an agent of its own on the outer run's async client, as the action guard does."""

        def before_tool(self, call: ToolCall) -> ToolDecision:
            threads.append(threading.current_thread())
            runner.run(inner_spec, {"diff": "+ inner"}, trace, client=client)
            return super().before_tool(call)

    async def outer() -> None:
        threads.append(threading.current_thread())
        await arun(spec, PAYLOAD, trace, repo_path="/repo", hook=Nested(), client=client)

    asyncio.run(outer())
    loop_thread, hook_thread = threads
    assert hook_thread is not loop_thread
    assert api.bodies[1]["tools"] == []
    assert api.bodies[1]["messages"] == [{"role": "user", "content": '{"diff": "+ inner"}'}]
    inputs = sorted(json.loads(row["input_json"])["diff"] for row in rows(trace))
    assert inputs == ["+ inner", "+ x"]


# --- runs gathered on one loop -----------------------------------------------------------------


def test_three_gathered_runs_write_three_rows(spec: LLMSpec, trace: Trace) -> None:
    async def three() -> list[Any]:
        gate = asyncio.Barrier(3)
        return await asyncio.gather(
            *(
                arun(
                    spec,
                    {"diff": f"+ {n}"},
                    trace,
                    repo_path="/repo",
                    pipeline_run_id="pipe-1",
                    client=FakeAsyncAPI(answer(), gate=gate).async_client(),
                )
                for n in range(3)
            )
        )

    outputs = asyncio.run(three())
    assert [output.model_dump() for output in outputs] == [{"ok": True}] * 3
    found = rows(trace)
    assert len({row["run_id"] for row in found}) == 3
    assert sorted(json.loads(row["input_json"])["diff"] for row in found) == ["+ 0", "+ 1", "+ 2"]
    per_run = cost_usd(MODEL, usage_of())
    assert per_run is not None
    assert trace.cost_by_pipeline_run("pipe-1") == pytest.approx(3 * per_run)


def test_fifty_gathered_runs_with_hooks_writing_from_threads_lose_no_rows(
    spec: LLMSpec, trace: Trace, read_file: None
) -> None:
    class Writer(RecordingHook):
        """Writes a row of its own from the worker thread its tool call runs on."""

        def after_tool(self, call: ToolCall, result: ToolResult) -> None:
            trace.record(extra_row("hook@test"))
            super().after_tool(call, result)

    async def fifty() -> None:
        await asyncio.gather(
            *(
                arun(
                    spec,
                    {"diff": f"+ {n}"},
                    trace,
                    repo_path="/repo",
                    hook=Writer(),
                    client=FakeAsyncAPI(asks_for_tool(), answer()).async_client(),
                )
                for n in range(50)
            )
        )

    asyncio.run(fifty())
    found = rows(trace)
    assert len(found) == 100
    assert sum(row["agent_ref"] == "hook@test" for row in found) == 50
    assert len({row["run_id"] for row in found}) == 100


def test_gathered_runs_overshoot_the_budget_by_at_most_one_message_each(
    spec: LLMSpec, trace: Trace
) -> None:
    budget = Budget(0.5)
    paused = message(thinking(), stop_reason="pause_turn", usage=cost_of(PRICED_INPUT))

    async def three() -> list[Any]:
        gate = asyncio.Barrier(3)
        return await asyncio.gather(
            *(
                arun(
                    spec,
                    PAYLOAD,
                    trace,
                    repo_path="/repo",
                    budget=budget,
                    client=FakeAsyncAPI(
                        paused, paused, answer(usage=cost_of(PRICED_INPUT)), gate=gate
                    ).async_client(),
                )
                for _ in range(3)
            ),
            return_exceptions=True,
        )

    outcomes = asyncio.run(three())
    assert all(isinstance(outcome, BudgetExceeded) for outcome in outcomes)
    per_message = cost_usd(MODEL, usage_of(PRICED_INPUT))
    assert per_message is not None
    # Three messages pass the $0.50 cap; the two other runs in flight each charge one more and
    # stop. Charging each run once at its end would have let all nine through ($1.80).
    assert budget.spent == pytest.approx(5 * per_message)
    assert budget.spent <= budget.usd + 3 * per_message
    found = rows(trace)
    assert len(found) == 3
    assert all(row["error"].startswith("BudgetExceeded: ") for row in found)
    assert sum(row["cost_usd"] for row in found) == pytest.approx(budget.spent)


# --- the trace lock ----------------------------------------------------------------------------


def test_trace_serialises_writes_and_reads_from_many_threads(trace: Trace) -> None:
    def work(n: int) -> None:
        for _ in range(25):
            trace.record(extra_row(f"agent-{n}@1.0.0"))
            trace.cost_by_agent()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    assert len(rows(trace)) == 200
    assert trace.cost_by_agent() == {f"agent-{n}@1.0.0": 0.0 for n in range(8)}
