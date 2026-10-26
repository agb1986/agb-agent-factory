"""Tests for evals/graders/llm_judge.py, against a fake Messages client."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx2
import pytest

from evals.cases import Expected
from evals.graders.llm_judge import JUDGE_MODEL, JUDGE_REF, JudgeError, LLMJudge, build_request
from factory.exit import AuthOrConfigError
from runtime.budget import Budget, BudgetExceeded
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace

EXPECTED = Expected(verdict="approve")
ACTUAL = {"verdict": "approve", "findings": [], "summary": "fine"}
GOOD = json.dumps({"pass": True, "score": 0.8, "reason": "clear"})


def _reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class FakeClient:
    def __init__(self, *replies: SimpleNamespace | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self.closed = False

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(json.loads(json.dumps(kwargs)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self) -> None:
        self.closed = True


def _judge(trace: Trace, client: FakeClient, budget: Budget | None = None) -> LLMJudge:
    return LLMJudge(
        trace,
        tag="eval:x@1.0.0",
        pipeline_run_id="run-1",
        client=cast(anthropic.Anthropic, client),
        budget=budget,
    )


REPLY_COST = cost_usd(JUDGE_MODEL, TokenUsage(100, 20, 0, 0)) or 0.0


@pytest.fixture
def trace(tmp_path: Path) -> Trace:
    return Trace(tmp_path / "traces.duckdb")


def _rows(trace: Trace) -> list[tuple[Any, ...]]:
    return (
        trace._connect()
        .execute(
            "SELECT agent_ref, kind, tag, pipeline_run_id, error FROM traces ORDER BY started_at"
        )
        .fetchall()
    )


def test_the_request_uses_the_judge_model_and_structured_output_without_sampling() -> None:
    request = build_request([{"role": "user", "content": "x"}])
    assert request["model"] == JUDGE_MODEL == "claude-haiku-5-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert set(request["output_config"]["format"]["schema"]["properties"]) == {
        "pass",
        "score",
        "reason",
    }
    assert not {"temperature", "top_p", "top_k", "tool_choice", "tools"} & set(request)


def test_a_valid_reply_is_the_verdict_and_is_traced(trace: Trace) -> None:
    client = FakeClient(_reply(GOOD))
    verdict = _judge(trace, client)(EXPECTED, ACTUAL)
    assert (verdict.passed, verdict.score, verdict.reason) == (True, 0.8, "clear")
    sent = json.loads(client.requests[0]["messages"][0]["content"])
    assert sent == {"expected": {"verdict": "approve", "defects": []}, "actual": ACTUAL}
    assert _rows(trace) == [(JUDGE_REF, "llm", "eval:x@1.0.0", "run-1", None)]


def test_the_judge_sees_criteria_and_no_null_verdict(trace: Trace) -> None:
    client = FakeClient(_reply(GOOD))
    expected = Expected(verdict=None, criteria="The rationale names the failed reviewer.")
    judgement = {"scores": {}, "merged_findings": [], "rationale": "r"}
    _judge(trace, client)(expected, judgement)
    sent = json.loads(client.requests[0]["messages"][0]["content"])
    assert sent["expected"] == {
        "defects": [],
        "criteria": "The rationale names the failed reviewer.",
    }
    assert sent["actual"] == judgement


def test_the_judge_prompt_explains_judgements_and_criteria() -> None:
    system = build_request([{"role": "user", "content": "x"}])["system"]
    assert "merged_findings" in system
    assert "criteria" in system


def test_the_judge_prompt_tells_it_the_sandbox_already_ran_the_tests() -> None:
    system = build_request([{"role": "user", "content": "x"}])["system"]
    assert "commands_run" in system
    assert "sandbox" in system


def test_an_invalid_reply_is_retried_once_with_the_errors(trace: Trace) -> None:
    client = FakeClient(_reply('{"pass": true, "score": 7, "reason": "r"}'), _reply(GOOD))
    verdict = _judge(trace, client)(EXPECTED, ACTUAL)
    assert verdict.passed
    retry = client.requests[1]["messages"]
    assert [message["role"] for message in retry] == ["user", "assistant", "user"]
    assert "score" in retry[2]["content"]
    assert len(_rows(trace)) == 1


def test_two_invalid_replies_raise_a_typed_error_and_trace_it(trace: Trace) -> None:
    client = FakeClient(_reply("nope"), _reply("still nope"))
    with pytest.raises(JudgeError, match="invalid verdict twice"):
        _judge(trace, client)(EXPECTED, ACTUAL)
    assert len(client.requests) == 2
    assert "JudgeError" in (_rows(trace)[0][4] or "")


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_a_refusal_or_a_cut_off_reply_raises_without_retry(trace: Trace, stop_reason: str) -> None:
    client = FakeClient(_reply("", stop_reason))
    with pytest.raises(JudgeError):
        _judge(trace, client)(EXPECTED, ACTUAL)
    assert len(client.requests) == 1


def test_a_rejected_key_is_an_auth_error(trace: Trace) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(401, request=request)
    error = anthropic.AuthenticationError("bad key", response=response, body=None)
    with pytest.raises(AuthOrConfigError):
        _judge(trace, FakeClient(error))(EXPECTED, ACTUAL)


def test_a_missing_key_is_an_auth_error_before_any_call(
    trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AuthOrConfigError, match="ANTHROPIC_API_KEY"):
        LLMJudge(trace)(EXPECTED, ACTUAL)


def test_close_leaves_a_client_that_was_passed_in_open(trace: Trace) -> None:
    client = FakeClient()
    _judge(trace, client).close()
    assert not client.closed


def test_a_call_is_charged_to_the_budget(trace: Trace) -> None:
    budget = Budget(1.0)
    _judge(trace, FakeClient(_reply(GOOD)), budget)(EXPECTED, ACTUAL)
    assert REPLY_COST > 0
    assert budget.spent == pytest.approx(REPLY_COST)


def test_a_retried_call_is_charged_for_both_replies(trace: Trace) -> None:
    budget = Budget(1.0)
    client = FakeClient(_reply('{"pass": true, "score": 7, "reason": "r"}'), _reply(GOOD))
    _judge(trace, client, budget)(EXPECTED, ACTUAL)
    assert budget.spent == pytest.approx(2 * REPLY_COST)


def test_a_spent_budget_refuses_the_call_before_anything_is_sent_or_traced(trace: Trace) -> None:
    budget = Budget(1.0)
    budget.add(1.0)
    client = FakeClient(_reply(GOOD))
    with pytest.raises(BudgetExceeded):
        _judge(trace, client, budget)(EXPECTED, ACTUAL)
    assert client.requests == []
    trace.ensure_schema()
    assert _rows(trace) == []


def test_a_call_that_passes_the_cap_is_traced_then_raises(trace: Trace) -> None:
    budget = Budget(REPLY_COST / 2)
    with pytest.raises(BudgetExceeded):
        _judge(trace, FakeClient(_reply(GOOD)), budget)(EXPECTED, ACTUAL)
    assert [row[4] for row in _rows(trace)] == [None]
    assert budget.spent == pytest.approx(REPLY_COST)


def test_a_failed_call_only_adds_its_cost_so_its_error_is_kept(trace: Trace) -> None:
    budget = Budget(REPLY_COST / 2)
    with pytest.raises(JudgeError):
        _judge(trace, FakeClient(_reply("nope"), _reply("still nope")), budget)(EXPECTED, ACTUAL)
    assert budget.spent == pytest.approx(2 * REPLY_COST)


def test_a_refused_call_is_traced_and_charged_for_its_reply(trace: Trace) -> None:
    budget = Budget(1.0)
    with pytest.raises(JudgeError):
        _judge(trace, FakeClient(_reply("", stop_reason="refusal")), budget)(EXPECTED, ACTUAL)
    (tokens,) = trace._connect().execute("SELECT input_tokens FROM traces").fetchall()
    assert tokens == (100,)
    assert budget.spent == pytest.approx(REPLY_COST)
