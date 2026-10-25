"""Tests for runtime/runner.py, against a mocked Messages API."""

import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from factory.exit import AuthOrConfigError, FactoryError, UsageError
from registry.resolver import load_spec
from registry.spec import LLMSpec
from runtime import runner
from runtime.errors import (
    AgentTimeoutError,
    ModelAPIError,
    OutputTruncatedError,
    OutputValidationError,
    RefusalError,
    ToolCrashError,
    TurnLimitError,
)
from runtime.hooks import ToolCall, ToolDecision, ToolResult
from runtime.pricing import TokenUsage, cost_usd
from runtime.runner import FORBIDDEN_REQUEST_KEYS, build_request, run
from runtime.trace import Trace
from tools import TOOLS
from tools._common import ToolNotFoundError, ToolUnavailableError

MODEL = "claude-sonnet-5-5"

SCHEMAS = """\
from pydantic import BaseModel


class In(BaseModel):
    diff: str


class Out(BaseModel):
    ok: bool
"""

MANIFEST = """\
name: reviewer
version: 1.0.0
kind: llm
description: Test agent.
model: {model}
effort: high
system_prompt: system.md
schemas: schemas.py
input_model: In
output_model: Out
tools:
  allow: [{allow}]
  deny: []
limits:
  max_tool_turns: {max_tool_turns}
  timeout_s: {timeout_s}
  max_output_tokens: 1024
evals:
  min_pass_rate: 0.75
"""

SYSTEM_PROMPT = "You review diffs.\n"


# --- canned API replies ------------------------------------------------------------------------


def message(
    *content: dict[str, Any],
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            **(usage or {}),
        },
        "content": list(content),
        **extra,
    }


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def thinking(signature: str = "sig-1") -> dict[str, Any]:
    return {"type": "thinking", "thinking": "Let me look.", "signature": signature}


def tool_use(
    tool_id: str = "tu_1", name: str = "git__read_file", **arguments: Any
) -> dict[str, Any]:
    arguments = arguments or {"path": "a.txt", "ref": "HEAD"}
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def answer(body: str = '{"ok": true}', **kwargs: Any) -> dict[str, Any]:
    return message(thinking(), text(body), **kwargs)


def asks_for_tool(tool_id: str = "tu_1", **arguments: Any) -> dict[str, Any]:
    return message(thinking(), tool_use(tool_id, **arguments), stop_reason="tool_use")


class FakeAPI:
    """A Messages API that answers each request with the next canned reply and keeps the bodies."""

    def __init__(self, *replies: dict[str, Any] | httpx2.Response | Exception) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        return httpx2.Response(200, json=reply)

    def client(self) -> anthropic.Anthropic:
        transport = httpx2.MockTransport(self.handle)
        return anthropic.Anthropic(
            api_key="test-key",
            http_client=anthropic.DefaultHttpxClient(transport=transport),
            max_retries=0,
        )


def tool_results(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the tool_result blocks of the last message in a request body."""
    return [block for block in body["messages"][-1]["content"] if block["type"] == "tool_result"]


# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def make_spec(tmp_path: Path) -> Callable[..., LLMSpec]:
    """Write the test agent and load it; keywords fill the manifest template."""

    def make(
        *,
        allow: str = "git.read_file",
        max_tool_turns: int = 4,
        timeout_s: int = 60,
        model: str = MODEL,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> LLMSpec:
        directory = tmp_path / "agents" / "reviewer"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "agent.yaml").write_text(
            MANIFEST.format(
                allow=allow, max_tool_turns=max_tool_turns, timeout_s=timeout_s, model=model
            )
        )
        (directory / "system.md").write_text(system_prompt)
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
def read_file_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace git.read_file with a fake that records its calls and returns `contents of <path>`."""
    calls: list[dict[str, Any]] = []

    def fake(repo_path: Any, *, max_bytes: int, **arguments: Any) -> str:
        calls.append({"repo_path": repo_path, "max_bytes": max_bytes, **arguments})
        return f"contents of {arguments['path']}"

    monkeypatch.setitem(TOOLS, "git.read_file", (TOOLS["git.read_file"][0], fake))
    return calls


def replace_read_file(monkeypatch: pytest.MonkeyPatch, impl: Callable[..., str]) -> None:
    monkeypatch.setitem(TOOLS, "git.read_file", (TOOLS["git.read_file"][0], impl))


def rows(trace: Trace) -> list[dict[str, Any]]:
    trace.ensure_schema()
    cursor = trace._connect().execute(
        "SELECT run_id, agent_ref, kind, pipeline_run_id, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, cost_usd, output_json, error, input_json "
        "FROM traces"
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]


def go(spec: LLMSpec, api: FakeAPI, trace: Trace, **kwargs: Any) -> BaseModel:
    kwargs.setdefault("repo_path", "/repo")
    return run(spec, {"diff": "+ x"}, trace, client=api.client(), **kwargs)


# --- the request ---------------------------------------------------------------------------------


def test_build_request_follows_the_conventions(spec: LLMSpec) -> None:
    request = build_request(spec, [{"role": "user", "content": "hi"}])
    assert FORBIDDEN_REQUEST_KEYS.isdisjoint(request)
    assert request["model"] == MODEL
    assert request["max_tokens"] == 1024
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"]["required"] == ["ok"]
    assert request["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    assert request["thread"] is False
    assert request["timeout"] == 60


def test_sent_body_has_no_forbidden_keys(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(answer())
    go(spec, api, trace)
    [body] = api.bodies
    assert FORBIDDEN_REQUEST_KEYS.isdisjoint(body)
    assert body["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(body["thinking"])
    assert body["output_config"]["effort"] == "high"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [tool["name"] for tool in body["tools"]] == ["git__read_file"]


# --- success -------------------------------------------------------------------------------------


def test_happy_path_returns_the_output_model_and_writes_one_row(
    spec: LLMSpec, trace: Trace
) -> None:
    api = FakeAPI(answer())
    output = go(spec, api, trace, pipeline_run_id="pipe-1")
    assert isinstance(output, spec.output_class)
    assert output.model_dump() == {"ok": True}
    assert len(api.bodies) == 1
    assert api.bodies[0]["messages"] == [{"role": "user", "content": '{"diff": "+ x"}'}]
    [row] = rows(trace)
    assert row["agent_ref"] == "reviewer@1.0.0"
    assert row["kind"] == "llm"
    assert row["pipeline_run_id"] == "pipe-1"
    assert json.loads(row["input_json"]) == {"diff": "+ x"}
    assert json.loads(row["output_json"]) == {"ok": True}
    assert row["error"] is None


def test_payload_may_be_a_model_instance(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(answer())
    output = run(
        spec,
        spec.input_class.model_validate({"diff": "+ y"}),
        trace,
        repo_path="/repo",
        client=api.client(),
    )
    assert output.model_dump() == {"ok": True}
    assert api.bodies[0]["messages"][0]["content"] == '{"diff": "+ y"}'


def test_agent_without_tools_needs_no_repo_path(
    make_spec: Callable[..., LLMSpec], trace: Trace
) -> None:
    api = FakeAPI(answer())
    run(make_spec(allow=""), {"diff": "+ x"}, trace, client=api.client())
    assert api.bodies[0]["tools"] == []


def test_tool_call_then_output(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    api = FakeAPI(asks_for_tool(), answer())
    output = go(spec, api, trace, repo_path="/the/repo")
    assert output.model_dump() == {"ok": True}
    assert read_file_calls == [
        {"repo_path": "/the/repo", "max_bytes": 65536, "path": "a.txt", "ref": "HEAD"}
    ]
    second = api.bodies[1]
    [result] = tool_results(second)
    assert result["tool_use_id"] == "tu_1"
    assert result["content"] == "contents of a.txt"
    assert not result.get("is_error")


def test_thinking_blocks_go_back_with_their_signatures(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    api = FakeAPI(asks_for_tool(), answer())
    go(spec, api, trace)
    assistant = api.bodies[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == thinking()
    assert assistant["content"][1]["type"] == "tool_use"


def test_usage_is_summed_into_the_trace_row(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    api = FakeAPI(
        message(
            thinking(),
            tool_use(),
            stop_reason="tool_use",
            usage={"cache_creation_input_tokens": 100},
        ),
        answer(usage={"input_tokens": 20, "output_tokens": 7, "cache_read_input_tokens": 100}),
    )
    go(spec, api, trace)
    [row] = rows(trace)
    usage = TokenUsage(
        input_tokens=30, output_tokens=12, cache_read_tokens=100, cache_write_tokens=100
    )
    assert (
        row["input_tokens"],
        row["output_tokens"],
        row["cache_read_tokens"],
        row["cache_write_tokens"],
    ) == (30, 12, 100, 100)
    assert row["cost_usd"] == pytest.approx(cost_usd(MODEL, usage))


def test_real_tool_reads_from_the_repository(spec: LLMSpec, trace: Trace, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("hello from the repo\n")
    for args in (
        ["init", "-q"],
        ["add", "a.txt"],
        ["-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         "commit", "-qm", "first"],
    ):  # fmt: skip
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    api = FakeAPI(asks_for_tool(), answer())
    go(spec, api, trace, repo_path=repo)
    [result] = tool_results(api.bodies[1])
    assert result["content"] == "hello from the repo\n"


# --- validation retry ------------------------------------------------------------------------------


def test_validation_failure_then_success(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(answer('{"ok": "maybe"}'), answer())
    output = go(spec, api, trace)
    assert output.model_dump() == {"ok": True}
    assert len(api.bodies) == 2
    retry = api.bodies[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert retry[1]["content"][0] == thinking()
    assert "does not validate against Out" in retry[2]["content"]
    assert "ok:" in retry[2]["content"]
    [row] = rows(trace)
    assert row["error"] is None


def test_two_validation_failures_raise_with_the_transcript(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(answer("not json"), answer('{"ok": "maybe"}'))
    with pytest.raises(OutputValidationError) as info:
        go(spec, api, trace)
    assert len(api.bodies) == 2
    assert info.value.agent == "reviewer@1.0.0"
    assert [m["role"] for m in info.value.transcript] == ["user", "assistant", "user", "assistant"]
    assert info.value.transcript[-1]["content"][1] == text('{"ok": "maybe"}')
    [row] = rows(trace)
    assert row["error"].startswith("OutputValidationError: ")
    assert row["output_json"] is None


# --- limits and stop reasons -----------------------------------------------------------------------


def test_turn_limit(
    make_spec: Callable[..., LLMSpec], trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    api = FakeAPI(asks_for_tool("tu_1"), asks_for_tool("tu_2"))
    with pytest.raises(TurnLimitError) as info:
        go(make_spec(max_tool_turns=1), api, trace)
    assert info.value.limit == 1
    assert len(api.bodies) == 2
    assert len(read_file_calls) == 1
    assert info.value.transcript[-1]["content"][1]["id"] == "tu_2"
    [row] = rows(trace)
    assert row["error"].startswith("TurnLimitError: ")


def test_refusal_is_not_retried(spec: LLMSpec, trace: Trace) -> None:
    refusal = message(
        text("I can't help with that."),
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "x"},
    )
    api = FakeAPI(refusal)
    with pytest.raises(RefusalError) as info:
        go(spec, api, trace)
    assert info.value.category == "cyber"
    assert len(api.bodies) == 1
    assert info.value.transcript[-1]["role"] == "assistant"
    assert rows(trace)[0]["error"].startswith("RefusalError: ")


def test_refusal_without_details_has_no_category(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(message(text(""), stop_reason="refusal"))
    with pytest.raises(RefusalError) as info:
        go(spec, api, trace)
    assert info.value.category is None


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
def test_truncated_output_is_not_retried(spec: LLMSpec, trace: Trace, stop_reason: str) -> None:
    api = FakeAPI(answer('{"ok": tr', stop_reason=stop_reason))
    with pytest.raises(OutputTruncatedError) as info:
        go(spec, api, trace)
    assert info.value.stop_reason == stop_reason
    assert len(api.bodies) == 1


def test_run_deadline_ends_the_run(
    make_spec: Callable[..., LLMSpec],
    trace: Trace,
    read_file_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    def slow(repo_path: Any, *, max_bytes: int, **arguments: Any) -> str:
        now[0] += 31
        return "slow"

    replace_read_file(monkeypatch, slow)
    api = FakeAPI(asks_for_tool(), answer())
    with pytest.raises(AgentTimeoutError) as info:
        go(make_spec(timeout_s=30), api, trace)
    assert info.value.timeout_s == 30
    assert len(api.bodies) == 1


def test_request_timeout_is_an_agent_timeout(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(httpx2.ReadTimeout("slow"))
    with pytest.raises(AgentTimeoutError):
        go(spec, api, trace)
    assert rows(trace)[0]["error"].startswith("AgentTimeoutError: ")


def test_paused_turn_is_resumed(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(message(thinking(), stop_reason="pause_turn"), answer())
    assert go(spec, api, trace).model_dump() == {"ok": True}
    assert len(api.bodies) == 2
    assert api.bodies[1]["messages"][-1] == {"role": "assistant", "content": [thinking()]}


def test_unexpected_stop_reason_is_a_model_api_error(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI(answer(stop_reason="compaction"))
    with pytest.raises(ModelAPIError, match="'compaction'"):
        go(spec, api, trace)
    assert len(api.bodies) == 1


def test_tool_use_stop_without_a_tool_call_is_a_model_api_error(
    spec: LLMSpec, trace: Trace
) -> None:
    api = FakeAPI(message(text("Let me check."), stop_reason="tool_use"))
    with pytest.raises(ModelAPIError, match="without a final answer"):
        go(spec, api, trace)
    assert len(api.bodies) == 1


def test_server_error_is_a_model_api_error(spec: LLMSpec, trace: Trace) -> None:
    error = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
    api = FakeAPI(httpx2.Response(500, json=error))
    with pytest.raises(ModelAPIError):
        go(spec, api, trace)


def test_rejected_key_is_an_auth_error(spec: LLMSpec, trace: Trace) -> None:
    error = {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    api = FakeAPI(httpx2.Response(401, json=error))
    with pytest.raises(AuthOrConfigError) as info:
        go(spec, api, trace)
    assert "ANTHROPIC_API_KEY" in info.value.message
    assert "test-key" not in f"{info.value.message} {info.value.hint}"


def test_missing_key_is_an_auth_error_naming_the_variable(
    spec: LLMSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AuthOrConfigError) as info:
        run(spec, {"diff": "+ x"}, trace, repo_path="/repo")
    assert info.value.message == "ANTHROPIC_API_KEY is not set"
    assert info.value.hint is not None
    assert "agb sys secrets set ANTHROPIC_API_KEY" in info.value.hint
    assert rows(trace)[0]["error"].startswith("AuthOrConfigError: ")


# --- tools and hooks ------------------------------------------------------------------------------


class RecordingHook:
    def __init__(self, deny: str | None = None) -> None:
        self.deny = deny
        self.before: list[ToolCall] = []
        self.after: list[tuple[ToolCall, ToolResult]] = []

    def before_tool(self, call: ToolCall) -> ToolDecision:
        self.before.append(call)
        return ToolDecision.allow() if self.deny is None else ToolDecision.deny(self.deny)

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        self.after.append((call, result))


def test_hook_sees_each_call_and_its_result(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    hook = RecordingHook()
    go(spec, FakeAPI(asks_for_tool(), answer()), trace, hook=hook)
    [call] = hook.before
    assert (call.name, dict(call.arguments), call.agent) == (
        "git.read_file",
        {"path": "a.txt", "ref": "HEAD"},
        "reviewer@1.0.0",
    )
    assert hook.after == [(call, ToolResult("contents of a.txt"))]


def test_hook_deny_becomes_the_tool_result(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    hook = RecordingHook(deny="reading a.txt is not allowed here")
    api = FakeAPI(asks_for_tool(), answer())
    assert go(spec, api, trace, hook=hook).model_dump() == {"ok": True}
    [result] = tool_results(api.bodies[1])
    assert result["content"] == "reading a.txt is not allowed here"
    assert result["is_error"] is True
    assert read_file_calls == []
    assert hook.after == []


class GuardStop(FactoryError):
    pass


def test_factory_error_from_a_hook_ends_the_run_unchanged(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    stop = GuardStop("three denials")

    class Stopper(RecordingHook):
        def before_tool(self, call: ToolCall) -> ToolDecision:
            raise stop

    api = FakeAPI(asks_for_tool(), answer())
    with pytest.raises(GuardStop) as info:
        go(spec, api, trace, hook=Stopper())
    assert info.value is stop
    assert len(api.bodies) == 1
    assert read_file_calls == []
    assert rows(trace)[0]["error"] == "GuardStop: three denials"


def test_other_hook_exception_is_a_tool_crash(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    class Broken(RecordingHook):
        def after_tool(self, call: ToolCall, result: ToolResult) -> None:
            raise KeyError("oops")

    api = FakeAPI(asks_for_tool(), answer())
    with pytest.raises(ToolCrashError) as info:
        go(spec, api, trace, hook=Broken())
    assert info.value.tool == "git.read_file"
    assert isinstance(info.value.__cause__, KeyError)
    assert len(api.bodies) == 1


def test_tool_crash_ends_the_run(
    spec: LLMSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*args: Any, **kwargs: Any) -> str:
        raise ZeroDivisionError("bug")

    replace_read_file(monkeypatch, crash)
    api = FakeAPI(asks_for_tool(), answer())
    with pytest.raises(ToolCrashError) as info:
        go(spec, api, trace)
    assert isinstance(info.value.__cause__, ZeroDivisionError)
    assert info.value.transcript[-1]["content"][0]["type"] == "tool_result"
    assert len(api.bodies) == 1


def test_calls_after_a_crash_in_the_same_turn_do_not_run(
    spec: LLMSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths: list[str] = []

    def crash(repo_path: Any, *, max_bytes: int, **arguments: Any) -> str:
        paths.append(arguments["path"])
        raise ZeroDivisionError("bug")

    replace_read_file(monkeypatch, crash)
    both = message(
        thinking(),
        tool_use("tu_1", path="a.txt", ref="HEAD"),
        tool_use("tu_2", path="b.txt", ref="HEAD"),
        stop_reason="tool_use",
    )
    api = FakeAPI(both, answer())
    with pytest.raises(ToolCrashError) as info:
        go(spec, api, trace)
    assert paths == ["a.txt"]
    results = info.value.transcript[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert "not run" in json.dumps(results[1]["content"])
    assert len(api.bodies) == 1


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
def test_tool_input_that_is_not_an_object_goes_back_to_the_model(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    odd = message({**tool_use(), "input": ["a.txt"]}, stop_reason="tool_use")
    api = FakeAPI(odd, answer())
    go(spec, api, trace)
    [result] = tool_results(api.bodies[1])
    assert result["content"] == "tool input must be a JSON object"
    assert result["is_error"] is True
    assert read_file_calls == []


def test_tool_error_goes_back_to_the_model(
    spec: LLMSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: Any, **kwargs: Any) -> str:
        raise ToolNotFoundError("no commit named 'nope'", hint="try HEAD")

    replace_read_file(monkeypatch, missing)
    api = FakeAPI(asks_for_tool(), answer())
    go(spec, api, trace)
    [result] = tool_results(api.bodies[1])
    assert result["content"] == "no commit named 'nope' (hint: try HEAD)"
    assert result["is_error"] is True


def test_missing_executable_ends_the_run(
    spec: LLMSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> str:
        raise ToolUnavailableError("git is not installed or not on PATH", hint="install git")

    replace_read_file(monkeypatch, unavailable)
    api = FakeAPI(asks_for_tool(), answer())
    with pytest.raises(ToolUnavailableError):
        go(spec, api, trace)
    assert len(api.bodies) == 1


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"path": "a.txt", "ref": "HEAD", "max_bytes": 10**9}, "unknown arguments: max_bytes"),
        ({"path": "a.txt"}, "missing required arguments: ref"),
        ({"path": "a.txt", "ref": 3}, "arguments must be strings: ref"),
    ],
)
def test_bad_arguments_go_back_to_the_model(
    spec: LLMSpec,
    trace: Trace,
    read_file_calls: list[dict[str, Any]],
    arguments: dict[str, Any],
    problem: str,
) -> None:
    api = FakeAPI(asks_for_tool(**arguments), answer())
    go(spec, api, trace)
    [result] = tool_results(api.bodies[1])
    assert result["content"].startswith(problem)
    assert result["is_error"] is True
    assert read_file_calls == []


def test_unknown_tool_name_goes_back_to_the_model(
    spec: LLMSpec, trace: Trace, read_file_calls: list[dict[str, Any]]
) -> None:
    unknown = message(tool_use(name="shell__run", cmd="ls"), stop_reason="tool_use")
    api = FakeAPI(unknown, answer())
    with pytest.warns(UserWarning, match="shell__run"):
        go(spec, api, trace)
    [result] = tool_results(api.bodies[1])
    assert "shell__run" in result["content"]
    assert result["is_error"] is True
    assert read_file_calls == []


# --- input checks ---------------------------------------------------------------------------------


def test_invalid_payload_is_a_usage_error_with_no_row(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI()
    with pytest.raises(UsageError) as info:
        run(spec, {"diff": ["SECRET-VALUE"]}, trace, repo_path="/repo", client=api.client())
    assert "diff:" in info.value.message
    assert "SECRET-VALUE" not in info.value.message
    assert api.bodies == []
    assert rows(trace) == []


def test_tools_without_repo_path_is_a_usage_error_with_no_row(spec: LLMSpec, trace: Trace) -> None:
    api = FakeAPI()
    with pytest.raises(UsageError):
        run(spec, {"diff": "+ x"}, trace, client=api.client())
    assert api.bodies == []
    assert rows(trace) == []
