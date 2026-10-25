"""Tests for the limits and the budget of runtime/runner.py: API retries, the token cap, `Budget`."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from factory.exit import FactoryError
from registry.resolver import load_spec
from registry.spec import LLMSpec
from runtime import runner
from runtime.budget import Budget, BudgetExceeded
from runtime.errors import ModelAPIError, RefusalError, TokenLimitError
from runtime.runner import run
from runtime.trace import Trace
from tests.runtime.test_runner import (
    MANIFEST,
    MODEL,
    SCHEMAS,
    SYSTEM_PROMPT,
    FakeAPI,
    answer,
    asks_for_tool,
    message,
    replace_read_file,
    rows,
    text,
)

PRICED_INPUT = 100_000
"""Input tokens that cost $0.20 on the test model at $2 per million."""


def api_error(status: int, **headers: str) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={"type": "error", "error": {"type": "api_error", "message": f"status {status}"}},
        headers=headers,
    )


class Sleeps:
    """A stand-in for `time.sleep` that records the waits."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[Trace]:
    with Trace(tmp_path / "trace.duckdb") as t:
        yield t


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def make_agent(tmp_path: Path) -> Callable[..., LLMSpec]:
    """Write the test agent with the given limits and load it."""

    def make(*, timeout_s: int = 60, **limits: int) -> LLMSpec:
        directory = tmp_path / "agents" / "reviewer"
        directory.mkdir(parents=True, exist_ok=True)
        manifest = MANIFEST.format(
            allow="git.read_file", max_tool_turns=4, timeout_s=timeout_s, model=MODEL
        )
        extra = "".join(f"  {name}: {value}\n" for name, value in limits.items())
        (directory / "agent.yaml").write_text(
            manifest.replace("  max_output_tokens: 1024\n", f"  max_output_tokens: 1024\n{extra}")
        )
        (directory / "system.md").write_text(SYSTEM_PROMPT)
        (directory / "schemas.py").write_text(SCHEMAS)
        spec = load_spec(directory / "agent.yaml")
        assert isinstance(spec, LLMSpec)
        return spec

    return make


def go(spec: LLMSpec, api: FakeAPI, trace: Trace, sleeps: Sleeps, **kwargs: Any) -> Any:
    return run(
        spec, {"diff": "+ x"}, trace, repo_path="/repo", client=api.client(), sleep=sleeps, **kwargs
    )


def cost_of(input_tokens: int) -> dict[str, int]:
    return {"input_tokens": input_tokens}


# --- API retries ---------------------------------------------------------------------------------


def test_max_retries_defaults_to_two(make_agent: Callable[..., LLMSpec]) -> None:
    assert make_agent().limits.max_retries == 2


def test_a_429_is_sent_again_and_the_run_succeeds(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(api_error(429), answer())
    output = go(make_agent(), api, trace, sleeps)
    assert output.model_dump() == {"ok": True}
    assert len(api.bodies) == 2
    assert api.bodies[0] == api.bodies[1]
    assert sleeps.waits == [0.5]
    [row] = rows(trace)
    assert row["error"] is None
    assert row["input_tokens"] == 10


@pytest.mark.parametrize(
    "failure",
    [
        api_error(429),
        api_error(500),
        api_error(503),
        api_error(529),
        httpx2.ConnectError("refused"),
    ],
    ids=["429", "500", "503", "529", "connection"],
)
def test_retryable_failures_are_sent_again(
    failure: httpx2.Response | Exception,
    make_agent: Callable[..., LLMSpec],
    trace: Trace,
    sleeps: Sleeps,
) -> None:
    api = FakeAPI(failure, answer())
    go(make_agent(), api, trace, sleeps)
    assert len(api.bodies) == 2
    assert sleeps.waits == [0.5]


@pytest.mark.parametrize("status", [400, 401, 404])
def test_other_statuses_are_not_retried(
    status: int, make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(api_error(status), answer())
    with pytest.raises(FactoryError):
        go(make_agent(), api, trace, sleeps)
    assert len(api.bodies) == 1
    assert sleeps.waits == []


def test_a_timeout_is_not_retried(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(httpx2.ReadTimeout("slow"), answer())
    with pytest.raises(FactoryError):
        go(make_agent(), api, trace, sleeps)
    assert len(api.bodies) == 1
    assert sleeps.waits == []


def test_waits_double_between_sends(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(api_error(529), api_error(529), api_error(529), answer())
    go(make_agent(max_retries=3), api, trace, sleeps)
    assert sleeps.waits == [0.5, 1.0, 2.0]


def test_retry_after_sets_the_wait_up_to_the_cap(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(api_error(429, **{"retry-after": "3"}), api_error(429, **{"retry-after": "99"}))
    api.replies.append(answer())
    go(make_agent(max_retries=2, timeout_s=600), api, trace, sleeps)
    assert sleeps.waits == [3.0, runner.MAX_BACKOFF_S]


def test_exhausted_retries_raise_with_the_transcript(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(api_error(429), api_error(429), api_error(429), answer())
    with pytest.raises(ModelAPIError) as info:
        go(make_agent(max_retries=2), api, trace, sleeps)
    assert len(api.bodies) == 3
    assert sleeps.waits == [0.5, 1.0]
    assert "after 2 retries" in info.value.message
    assert info.value.transcript == ({"role": "user", "content": '{"diff": "+ x"}'},)
    [row] = rows(trace)
    assert row["error"].startswith("ModelAPIError: ")


def test_zero_retries_means_one_send(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(api_error(500), answer())
    with pytest.raises(ModelAPIError) as info:
        go(make_agent(max_retries=0), api, trace, sleeps)
    assert len(api.bodies) == 1
    assert sleeps.waits == []
    assert "retries" not in info.value.message


def test_a_wait_past_the_deadline_is_not_started(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    api = FakeAPI(api_error(429, **{"retry-after": "30"}), answer())
    with pytest.raises(ModelAPIError) as info:
        go(make_agent(timeout_s=5), api, trace, sleeps)
    assert sleeps.waits == []
    assert len(api.bodies) == 1
    assert "limits.timeout_s" in info.value.message


def test_the_retry_count_restarts_after_a_reply(
    make_agent: Callable[..., LLMSpec],
    trace: Trace,
    sleeps: Sleeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace_read_file(monkeypatch, lambda repo_path, *, max_bytes, **arguments: "contents")
    api = FakeAPI(api_error(429), asks_for_tool(), api_error(429), answer())
    go(make_agent(max_retries=1), api, trace, sleeps)
    assert len(api.bodies) == 4
    assert sleeps.waits == [0.5, 0.5]
    # The resend after the tool turn carries the whole conversation so far.
    assert len(api.bodies[3]["messages"]) == 3


def test_the_default_client_does_not_retry_by_itself(
    make_agent: Callable[..., LLMSpec],
    trace: Trace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def stop(**kwargs: Any) -> Any:
        seen.append(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setenv(runner.API_KEY_ENV, "test-key")
    monkeypatch.setattr(runner.anthropic, "Anthropic", stop)
    with pytest.raises(RuntimeError):
        run(make_agent(), {"diff": "+ x"}, trace, repo_path="/repo")
    assert seen == [{"max_retries": 0}]


# --- max_tokens_total ----------------------------------------------------------------------------


def test_tokens_over_the_total_raise_with_the_partial_transcript(
    make_agent: Callable[..., LLMSpec],
    trace: Trace,
    sleeps: Sleeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace_read_file(monkeypatch, lambda repo_path, *, max_bytes, **arguments: "contents")
    # 15 tokens a turn: the second turn takes the run from 15 to 30, past the limit of 20.
    api = FakeAPI(asks_for_tool(), asks_for_tool("tu_2"), answer())
    with pytest.raises(TokenLimitError) as info:
        go(make_agent(max_tokens_total=20), api, trace, sleeps)
    assert info.value.limit == 20
    assert info.value.used == 30
    assert len(api.bodies) == 2
    roles = [entry["role"] for entry in info.value.transcript]
    assert roles == ["user", "assistant", "user", "assistant"]
    [row] = rows(trace)
    assert row["error"].startswith("TokenLimitError: ")
    assert row["input_tokens"] == 20
    assert row["output_tokens"] == 10


def test_cache_tokens_count_towards_the_total(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    reply = answer(usage={"cache_read_input_tokens": 100, "cache_creation_input_tokens": 50})
    with pytest.raises(TokenLimitError) as info:
        go(make_agent(max_tokens_total=100), FakeAPI(reply), trace, sleeps)
    assert info.value.used == 165


def test_tokens_at_the_total_pass(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    go(make_agent(max_tokens_total=15), FakeAPI(answer()), trace, sleeps)


def test_no_total_means_no_cap(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    go(make_agent(), FakeAPI(answer(usage=cost_of(10_000_000))), trace, sleeps)


# --- the budget ----------------------------------------------------------------------------------


def test_the_budget_stops_the_second_run_when_the_first_spent_most_of_it(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    spec = make_agent()
    budget = Budget(0.25)
    first = FakeAPI(answer(usage=cost_of(PRICED_INPUT)))
    go(spec, first, trace, sleeps, budget=budget)
    assert budget.spent == pytest.approx(0.2, abs=1e-4)

    second = FakeAPI(answer(usage=cost_of(PRICED_INPUT)))
    with pytest.raises(BudgetExceeded) as info:
        go(spec, second, trace, sleeps, budget=budget)
    assert info.value.budget_usd == 0.25
    assert info.value.spent_usd == pytest.approx(0.4, abs=1e-3)
    assert "$0.40" in info.value.message
    assert len(rows(trace)) == 2
    assert rows(trace)[-1]["error"].startswith("BudgetExceeded: ")


def test_a_run_that_would_pass_the_budget_stops_between_turns(
    make_agent: Callable[..., LLMSpec],
    trace: Trace,
    sleeps: Sleeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace_read_file(monkeypatch, lambda repo_path, *, max_bytes, **arguments: "contents")
    api = FakeAPI(message(text("x"), stop_reason="tool_use", usage=cost_of(PRICED_INPUT)), answer())
    with pytest.raises(BudgetExceeded):
        go(make_agent(), api, trace, sleeps, budget=Budget(0.1))
    assert len(api.bodies) == 1


def test_a_spent_budget_refuses_the_run_before_any_request(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    budget = Budget(0.1)
    budget.add(0.1)
    api = FakeAPI(answer())
    with pytest.raises(BudgetExceeded):
        go(make_agent(), api, trace, sleeps, budget=budget)
    assert api.bodies == []
    assert rows(trace) == []


def test_a_failed_run_is_charged_without_hiding_its_error(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    budget = Budget(5.0)
    refusal = message(
        text("no"), stop_reason="refusal", usage=cost_of(PRICED_INPUT), stop_details={}
    )
    with pytest.raises(RefusalError):
        go(make_agent(), FakeAPI(refusal), trace, sleeps, budget=budget)
    assert budget.spent == pytest.approx(0.2, abs=1e-4)


def test_no_budget_means_no_limit(
    make_agent: Callable[..., LLMSpec], trace: Trace, sleeps: Sleeps
) -> None:
    go(make_agent(), FakeAPI(answer(usage=cost_of(50_000_000))), trace, sleeps)
    assert len(rows(trace)) == 1
