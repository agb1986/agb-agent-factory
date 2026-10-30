"""Tests for runtime/jev_runner.py against a fake transport, and runtime/jev_transport.py against a
mocked HTTP layer."""

import functools
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from factory.exit import AuthOrConfigError, ExitCode, FactoryError, UsageError
from registry import JevSpec
from registry.resolver import resolve
from runtime import jev_runner, jev_transport
from runtime.budget import Budget, BudgetExceeded
from runtime.jev_runner import (
    API_KEY_ENV,
    MAX_ATTEMPTS,
    MAX_BACKOFF_S,
    MIN_CHARS,
    TIMEOUT_S,
    ChoiceResult,
    JevContextExceededError,
    JevError,
    JevQuestion,
    JevResponse,
    JevResult,
    JevTimeoutError,
    JevTransportError,
    NoulResult,
    ScoreResult,
    default_transport,
    run_jev,
)
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace

MODEL = "jev-1.14.0"
SECRET = "sk-test-secret-value"

SCORE_MANIFEST: dict[str, Any] = {
    "name": "sizer",
    "version": "1.0.0",
    "kind": "jev",
    "description": "Test Jev agent with a score question.",
    "model": MODEL,
    "input": {"render": "Diff:\n{diff}", "max_chars": 1000},
    "questions": [
        {
            "id": "size",
            "type": "score",
            "text": "How big is this change?",
            "range": [0, 100],
            "levels": ["small", "medium", "large"],
        }
    ],
    "evals": {"min_pass_rate": 0.9},
}


@pytest.fixture
def triage() -> JevSpec:
    spec = resolve("diff-triage", "^0.3")
    assert isinstance(spec, JevSpec)
    return spec


@pytest.fixture
def sizer() -> JevSpec:
    return JevSpec.model_validate({**SCORE_MANIFEST, "path": Path("agent.yaml")})


@pytest.fixture
def trace(tmp_path: Path) -> Any:
    with Trace(tmp_path / "trace.duckdb") as opened:
        yield opened


PAYLOAD = {"files": "a.py", "diff": "+x = 1"}


def triage_answers(p: float = 0.9, choice: str = "code") -> dict[str, JevResult]:
    others = {"code": 0.1, "code-security": 0.1, "code-perf": 0.1, "all": 0.1}
    others[choice] = 0.7
    return {
        "needs_review": NoulResult(noul=p),
        "reviewers": ChoiceResult(choice=choice, confidence=0.6, probabilities=others),
    }


def response(
    answers: dict[str, JevResult] | None = None, *, tokens: int | None = 120, model: str = MODEL
) -> JevResponse:
    return JevResponse(
        model=model,
        input_tokens=tokens,
        answers=triage_answers() if answers is None else answers,
    )


class FakeTransport:
    """Plays back replies in order, an exception being raised, and records every request."""

    def __init__(self, *replies: JevResponse | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def ask(
        self, *, model: str, state: str, questions: Sequence[JevQuestion], timeout_s: float
    ) -> JevResponse:
        self.calls.append(
            {"model": model, "state": state, "questions": tuple(questions), "timeout_s": timeout_s}
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Sleeps:
    """A `sleep` that records the waits instead of waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def rows(trace: Trace) -> list[dict[str, Any]]:
    trace.ensure_schema()
    cursor = trace._connect().execute(
        "SELECT run_id, agent_ref, kind, pipeline_run_id, input_tokens, output_tokens, "
        "cost_usd, decision_json, output_json, input_json, error, duration_ms FROM traces"
    )
    names = [column[0] for column in cursor.description]
    found = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
    for row in found:
        for key in ("decision_json", "output_json", "input_json"):
            if isinstance(row[key], str):
                row[key] = json.loads(row[key])
    return found


def go(
    spec: JevSpec,
    transport: FakeTransport,
    trace: Trace,
    payload: dict[str, Any] | BaseModel | None = None,
    sleeps: Sleeps | None = None,
    **kwargs: Any,
) -> BaseModel:
    return run_jev(
        spec,
        PAYLOAD if payload is None else payload,
        trace,
        transport=transport,
        sleep=sleeps if sleeps is not None else Sleeps(),
        **kwargs,
    )


def status_error(status: int, retry_after_s: float | None = None) -> JevTransportError:
    return JevTransportError(
        f"HTTP {status}",
        status=status,
        retryable=status == 429 or status >= 500,
        retry_after_s=retry_after_s,
    )


# --- the request ---------------------------------------------------------------------------------


def test_renders_the_template_and_sends_it_once_with_every_question(
    triage: JevSpec, trace: Trace
) -> None:
    transport = FakeTransport(response())
    go(triage, transport, trace)
    (call,) = transport.calls
    assert call["state"] == "Files changed:\na.py\n\nDiff:\n+x = 1"
    assert call["model"] == MODEL
    assert call["timeout_s"] == TIMEOUT_S == 5.0
    assert call["questions"] == (
        JevQuestion("needs_review", "noul", "Does this diff need human-quality review?"),
        JevQuestion(
            "reviewers",
            "choice",
            triage.questions[1].text,
            ("code", "code-security", "code-perf", "all"),
        ),
    )
    assert triage.questions[1].text.startswith("Which reviewers should see this diff?")


def test_a_model_payload_renders_like_a_dict(triage: JevSpec, trace: Trace) -> None:
    class Payload(BaseModel):
        files: str
        diff: str

    transport = FakeTransport(response())
    go(triage, transport, trace, Payload(files="a.py", diff="+x = 1"))
    assert transport.calls[0]["state"].endswith("Diff:\n+x = 1")


def test_a_score_question_sends_its_levels_in_order(sizer: JevSpec, trace: Trace) -> None:
    transport = FakeTransport(
        response({"size": ScoreResult(1.0, 0.5, _legend(3), _probs(0.2, 0.6, 0.2))})
    )
    go(sizer, transport, trace, {"diff": "+x"})
    assert transport.calls[0]["questions"] == (
        JevQuestion("size", "score", "How big is this change?", ("small", "medium", "large")),
    )


def test_input_over_max_chars_is_cut_with_a_marker_and_flagged(
    triage: JevSpec, trace: Trace
) -> None:
    transport = FakeTransport(response())
    go(triage, transport, trace, {"files": "a.py", "diff": "x" * 30_000})
    state = transport.calls[0]["state"]
    rendered_length = len(triage.input.render.format(files="a.py", diff="x" * 30_000))
    assert state.startswith("Files changed:\na.py\n\nDiff:\nxxx")
    body, marker = state.rsplit("\n", 1)
    assert len(body) == triage.input.max_chars == 24_000
    assert marker == f"[truncated: input cut to 24000 of {rendered_length} characters]"
    (row,) = rows(trace)
    assert row["decision_json"] == {"truncated": True, "model": MODEL}


def test_input_at_max_chars_is_not_cut(trace: Trace, sizer: JevSpec) -> None:
    transport = FakeTransport(
        response({"size": ScoreResult(1.0, 0.5, _legend(3), _probs(0.2, 0.6, 0.2))})
    )
    diff = "x" * (1000 - len("Diff:\n"))
    go(sizer, transport, trace, {"diff": diff})
    assert transport.calls[0]["state"] == f"Diff:\n{diff}"
    assert rows(trace)[0]["decision_json"]["truncated"] is False


# --- retries -------------------------------------------------------------------------------------


def test_a_429_is_retried_with_a_doubling_backoff_then_succeeds(
    triage: JevSpec, trace: Trace
) -> None:
    transport = FakeTransport(status_error(429), status_error(503), response())
    sleeps = Sleeps()
    output = go(triage, transport, trace, sleeps=sleeps)
    assert len(transport.calls) == 3
    assert sleeps.waits == [0.5, 1.0]
    assert output.model_dump()["needs_review"]["p"] == 0.9
    assert len(rows(trace)) == 1


def test_retry_after_is_honoured_up_to_the_cap(triage: JevSpec, trace: Trace) -> None:
    transport = FakeTransport(status_error(429, 2.5), status_error(429, 60.0), response())
    sleeps = Sleeps()
    go(triage, transport, trace, sleeps=sleeps)
    assert sleeps.waits == [2.5, MAX_BACKOFF_S]


def test_5xx_that_never_clears_ends_after_three_attempts(triage: JevSpec, trace: Trace) -> None:
    transport = FakeTransport(*(status_error(503) for _ in range(5)))
    sleeps = Sleeps()
    with pytest.raises(JevError, match="gave up after 3 attempts: HTTP 503") as caught:
        go(triage, transport, trace, sleeps=sleeps)
    assert len(transport.calls) == MAX_ATTEMPTS == 3
    assert sleeps.waits == [0.5, 1.0]
    assert isinstance(caught.value, FactoryError) and caught.value.code == ExitCode.ERROR
    (row,) = rows(trace)
    assert row["error"] == f"JevError: {caught.value}"
    assert row["output_json"] is None


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_statuses_are_not_retried(triage: JevSpec, trace: Trace, status: int) -> None:
    transport = FakeTransport(status_error(status), response())
    sleeps = Sleeps()
    with pytest.raises(JevError, match=f"HTTP {status}"):
        go(triage, transport, trace, sleeps=sleeps)
    assert len(transport.calls) == 1 and sleeps.waits == []


def test_a_timeout_is_a_jev_error_and_is_not_retried(triage: JevSpec, trace: Trace) -> None:
    transport = FakeTransport(JevTimeoutError("the request timed out after 5 s"), response())
    with pytest.raises(JevError, match="timed out after 5 s"):
        go(triage, transport, trace)
    assert len(transport.calls) == 1
    (row,) = rows(trace)
    assert row["error"].startswith("JevError: ")
    assert row["input_tokens"] is None


# --- parsing -------------------------------------------------------------------------------------


def test_noul_and_choice_answers_fill_the_output_model(triage: JevSpec, trace: Trace) -> None:
    output = go(triage, FakeTransport(response(triage_answers(0.25, "all"))), trace)
    assert isinstance(output, triage.output_class)
    data = output.model_dump()
    assert data["needs_review"] == {"p": 0.25}
    assert data["reviewers"]["answer"] == "all"
    assert data["reviewers"]["confidence"] == 0.6
    assert data["reviewers"]["probs"]["all"] == 0.7


def _legend(count: int) -> dict[str, str]:
    return {str(index): f"description {index}" for index in range(count)}


def _probs(*values: float) -> dict[str, float]:
    return {str(index): value for index, value in enumerate(values)}


def score(spec: JevSpec, trace: Trace, result: ScoreResult) -> dict[str, Any]:
    output = go(spec, FakeTransport(response({"size": result})), trace, {"diff": "+x"})
    return output.model_dump()["size"]


@pytest.mark.parametrize(
    ("expected_level", "value"),
    [(0.0, 0.0), (1.0, 50.0), (2.0, 100.0), (1.5, 75.0), (0.2, 10.0)],
)
def test_score_value_maps_the_expected_level_linearly_onto_range(
    sizer: JevSpec, trace: Trace, expected_level: float, value: float
) -> None:
    answer = score(
        sizer, trace, ScoreResult(expected_level, 0.4, _legend(3), _probs(0.3, 0.3, 0.4))
    )
    assert answer["value"] == pytest.approx(value)


def test_score_value_follows_a_range_that_does_not_start_at_zero(trace: Trace) -> None:
    manifest = {**SCORE_MANIFEST}
    manifest["questions"] = [{**SCORE_MANIFEST["questions"][0], "range": [1, 5]}]
    spec = JevSpec.model_validate({**manifest, "path": Path("agent.yaml")})
    answer = score(spec, trace, ScoreResult(1.0, 0.4, _legend(3), _probs(0.3, 0.4, 0.3)))
    assert answer["value"] == pytest.approx(3.0)


def test_score_level_is_the_likeliest_level_named_through_the_manifest(
    sizer: JevSpec, trace: Trace
) -> None:
    answer = score(sizer, trace, ScoreResult(1.6, 0.4, _legend(3), _probs(0.1, 0.3, 0.6)))
    assert answer["level"] == "large"
    assert answer["probs"] == {"small": 0.1, "medium": 0.3, "large": 0.6}
    assert answer["confidence"] == 0.4


def test_score_level_tie_goes_to_the_lower_level(sizer: JevSpec, trace: Trace) -> None:
    answer = score(sizer, trace, ScoreResult(1.0, 0.0, _legend(3), _probs(0.5, 0.0, 0.5)))
    assert answer["level"] == "small"


@pytest.mark.parametrize(
    "result",
    [
        ScoreResult(3.0, 0.5, _legend(3), _probs(0.3, 0.3, 0.4)),
        ScoreResult(-0.1, 0.5, _legend(3), _probs(0.3, 0.3, 0.4)),
        ScoreResult(1.0, 0.5, _legend(2), _probs(0.3, 0.3, 0.4)),
        ScoreResult(1.0, 0.5, _legend(3), _probs(0.5, 0.5)),
    ],
    ids=["score above the top level", "score below zero", "legend short", "probabilities short"],
)
def test_a_score_that_does_not_fit_the_levels_is_a_jev_error(
    sizer: JevSpec, trace: Trace, result: ScoreResult
) -> None:
    with pytest.raises(JevError, match="answer 'size' is malformed"):
        score(sizer, trace, result)
    assert rows(trace)[0]["error"].startswith("JevError: ")


def test_a_missing_answer_is_a_jev_error(triage: JevSpec, trace: Trace) -> None:
    answers = triage_answers()
    del answers["reviewers"]
    with pytest.raises(JevError, match="answer 'reviewers' is malformed: .*no answer"):
        go(triage, FakeTransport(response(answers)), trace)


def test_an_answer_of_the_wrong_type_is_a_jev_error(triage: JevSpec, trace: Trace) -> None:
    answers = triage_answers()
    answers["needs_review"] = ChoiceResult("a", 0.5, {"a": 1.0})
    with pytest.raises(JevError, match="expected a noul answer, got ChoiceResult"):
        go(triage, FakeTransport(response(answers)), trace)


def test_a_choice_the_manifest_does_not_list_is_a_jev_error(triage: JevSpec, trace: Trace) -> None:
    answers = triage_answers()
    answers["reviewers"] = ChoiceResult("nobody", 0.5, {"nobody": 1.0})
    with pytest.raises(JevError, match="do not match the output model: reviewers"):
        go(triage, FakeTransport(response(answers)), trace)


def test_a_noul_outside_zero_to_one_is_a_jev_error(triage: JevSpec, trace: Trace) -> None:
    with pytest.raises(JevError, match="do not match the output model: needs_review"):
        go(triage, FakeTransport(response(triage_answers(p=1.5))), trace)


# --- the trace row -------------------------------------------------------------------------------


def test_a_successful_run_writes_one_complete_row(triage: JevSpec, trace: Trace) -> None:
    output = go(triage, FakeTransport(response()), trace, pipeline_run_id="pipe-1")
    (row,) = rows(trace)
    assert row["kind"] == "jev"
    assert row["agent_ref"] == "diff-triage@0.3.0"
    assert row["pipeline_run_id"] == "pipe-1"
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 0
    assert row["cost_usd"] == pytest.approx(120 * 0.042 / 1_000_000)
    assert row["cost_usd"] == cost_usd(MODEL, TokenUsage(input_tokens=120))
    assert row["decision_json"] == {"truncated": False, "model": MODEL}
    assert row["output_json"] == output.model_dump(mode="json")
    assert row["output_json"]["reviewers"]["probs"]["code"] == 0.7
    assert row["input_json"] == PAYLOAD
    assert row["error"] is None
    assert row["duration_ms"] >= 0


def test_two_runs_have_distinct_run_ids(triage: JevSpec, trace: Trace) -> None:
    go(triage, FakeTransport(response(), response()), trace)
    go(triage, FakeTransport(response()), trace)
    ids = {row["run_id"] for row in rows(trace)}
    assert len(ids) == 2


def test_a_failed_run_writes_one_row_with_the_error(triage: JevSpec, trace: Trace) -> None:
    transport = FakeTransport(status_error(400))
    with pytest.raises(JevError):
        go(triage, transport, trace)
    (row,) = rows(trace)
    assert row["error"] == "JevError: diff-triage@0.3.0: HTTP 400"
    assert row["output_json"] is None
    assert row["decision_json"] == {"truncated": False, "model": None}
    assert row["cost_usd"] == cost_usd(MODEL, TokenUsage())


def test_a_response_without_usage_costs_nothing_and_says_so(triage: JevSpec, trace: Trace) -> None:
    go(triage, FakeTransport(response(tokens=None)), trace)
    (row,) = rows(trace)
    assert row["input_tokens"] is None
    assert row["cost_usd"] == 0.0


def test_a_response_from_another_model_is_recorded_and_priced_as_that_model(
    triage: JevSpec, trace: Trace
) -> None:
    go(triage, FakeTransport(response(model="jev-9.9.9")), trace)
    (row,) = rows(trace)
    assert row["decision_json"]["model"] == "jev-9.9.9"
    assert row["cost_usd"] is None


# --- bad payloads --------------------------------------------------------------------------------


def test_a_payload_missing_a_template_field_is_a_usage_error_and_untraced(
    triage: JevSpec, trace: Trace
) -> None:
    transport = FakeTransport(response())
    with pytest.raises(UsageError, match="missing diff") as caught:
        go(triage, transport, trace, {"files": SECRET})
    assert SECRET not in str(caught.value) and SECRET not in str(caught.value.hint)
    assert transport.calls == []
    assert rows(trace) == []


def test_a_payload_that_is_not_json_is_a_usage_error_and_untraced(
    triage: JevSpec, trace: Trace
) -> None:
    with pytest.raises(UsageError, match="not JSON-serializable"):
        go(triage, FakeTransport(), trace, {"files": "a", "diff": object()})
    assert rows(trace) == []


# --- keys and the SDK ----------------------------------------------------------------------------


def test_a_key_the_transport_reports_as_rejected_is_an_auth_error_and_traced(
    triage: JevSpec, trace: Trace
) -> None:
    rejected = AuthOrConfigError("the Jev API rejected TYPESAFE_API_KEY", hint=jev_runner.KEY_HINT)
    with pytest.raises(AuthOrConfigError) as caught:
        go(triage, FakeTransport(rejected), trace)
    assert caught.value.code == ExitCode.AUTH_OR_CONFIG
    assert rows(trace)[0]["error"].startswith("AuthOrConfigError: ")


def test_a_missing_key_is_an_auth_error_at_call_time(
    triage: JevSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with pytest.raises(AuthOrConfigError, match=API_KEY_ENV) as caught:
        run_jev(triage, PAYLOAD, trace)
    assert caught.value.code == ExitCode.AUTH_OR_CONFIG
    assert API_KEY_ENV in str(caught.value.hint)
    assert len(rows(trace)) == 1


def test_a_missing_sdk_is_an_auth_error_at_call_time_not_import_time(
    triage: JevSpec, trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    monkeypatch.delitem(sys.modules, "runtime.jev_transport")
    with pytest.raises(AuthOrConfigError, match="typesafe-sdk") as caught:
        run_jev(triage, PAYLOAD, trace)
    assert "uv sync --extra jev" in str(caught.value.hint)
    assert SECRET not in str(caught.value) + str(caught.value.hint)


def test_importing_the_runner_does_not_import_the_sdk() -> None:
    import subprocess

    code = "import sys, runtime.jev_runner; print('typesafe_sdk' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "False"


# --- 400 max_tokens_exceeded ---------------------------------------------------------------------


def rendered_length(spec: JevSpec, diff: str) -> int:
    return len(spec.input.render.format(files="a.py", diff=diff))


def test_a_request_over_the_context_is_cut_in_half_and_sent_again_as_truncated(
    triage: JevSpec, trace: Trace
) -> None:
    transport = FakeTransport(JevContextExceededError("HTTP 400", status=400), response())
    go(triage, transport, trace, {"files": "a.py", "diff": "x" * 20_000})
    total = rendered_length(triage, "x" * 20_000)
    first, second = (call["state"] for call in transport.calls)
    assert len(first) == total and "[truncated" not in first
    body, marker = second.rsplit("\n", 1)
    assert len(body) == total // 2
    assert marker == f"[truncated: input cut to {total // 2} of {total} characters]"
    (row,) = rows(trace)
    assert row["decision_json"]["truncated"] is True
    assert row["error"] is None


def test_the_context_retry_keeps_halving_until_it_fits(triage: JevSpec, trace: Trace) -> None:
    transport = FakeTransport(
        JevContextExceededError("HTTP 400", status=400),
        JevContextExceededError("HTTP 400", status=400),
        response(),
    )
    go(triage, transport, trace, {"files": "a.py", "diff": "x" * 20_000})
    total = rendered_length(triage, "x" * 20_000)
    lengths = [len(call["state"].rsplit("\n", 1)[0]) for call in transport.calls[1:]]
    assert lengths == [total // 2, total // 4]


def test_a_request_that_never_fits_ends_as_a_jev_error_at_the_floor(
    triage: JevSpec, trace: Trace
) -> None:
    transport = FakeTransport(*(JevContextExceededError("HTTP 400", status=400) for _ in range(20)))
    with pytest.raises(JevError, match=f"cut to {MIN_CHARS} characters"):
        go(triage, transport, trace, {"files": "a.py", "diff": "x" * 20_000})
    assert min(len(call["state"]) for call in transport.calls) >= MIN_CHARS // 2
    (row,) = rows(trace)
    assert row["decision_json"]["truncated"] is True


def test_default_transport_is_the_sdk_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    assert isinstance(default_transport(), jev_transport.TypeSafeTransport)


# --- the SDK transport, against a mocked HTTP layer ----------------------------------------------


def wire_answers() -> dict[str, Any]:
    return {
        "needs_review": {"type": "noul", "noul": 0.81},
        "reviewers": {
            "type": "choice",
            "choice": "code-perf",
            "confidence": 0.5,
            "probabilities": {"code": 0.1, "code-security": 0.1, "code-perf": 0.7, "all": 0.1},
        },
        "size": {
            "type": "score",
            "score": 1.25,
            "confidence": 0.3,
            "legend": {"0": "a", "1": "b", "2": "c"},
            "probabilities": {"0": 0.2, "1": 0.5, "2": 0.3},
        },
    }


@pytest.fixture
def http(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[httpx2.Request], httpx2.Response]], list[httpx2.Request]]:
    """Point the SDK client at `handler`; returns the list that collects the requests."""
    monkeypatch.setenv(API_KEY_ENV, SECRET)

    def install(handler: Callable[[httpx2.Request], httpx2.Response]) -> list[httpx2.Request]:
        seen: list[httpx2.Request] = []

        def record(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(
            jev_transport,
            "TypeSafeClient",
            functools.partial(jev_transport.TypeSafeClient, transport=httpx2.MockTransport(record)),
        )
        return seen

    return install


QUESTIONS = (
    JevQuestion("needs_review", "noul", "Needs review?"),
    JevQuestion("reviewers", "choice", "Who?", ("code", "code-security", "code-perf", "all")),
    JevQuestion("size", "score", "How big?", ("small", "medium", "large")),
)


def ask() -> JevResponse:
    return jev_transport.TypeSafeTransport().ask(
        model=MODEL, state="the diff", questions=QUESTIONS, timeout_s=5.0
    )


def test_transport_sends_one_request_and_maps_every_answer_type(http: Any) -> None:
    body = {
        "model": MODEL,
        "answers": wire_answers(),
        "usage": {"input_tokens": 77, "output_tokens": 3},
    }
    seen = http(lambda request: httpx2.Response(200, json=body))
    result = ask()
    (request,) = seen
    sent = json.loads(request.content)
    assert sent["state"] == "the diff"
    assert sent["model"] == MODEL
    assert list(sent["questions"]) == ["needs_review", "reviewers", "size"]
    assert sent["questions"]["size"]["criteria"] == ["small", "medium", "large"]
    assert sent["questions"]["reviewers"]["criteria"] == {
        "code": None,
        "code-security": None,
        "code-perf": None,
        "all": None,
    }
    assert result.model == MODEL and result.input_tokens == 77
    assert result.answers["needs_review"] == NoulResult(0.81)
    assert result.answers["reviewers"] == ChoiceResult(
        "code-perf",
        0.5,
        {"code": 0.1, "code-security": 0.1, "code-perf": 0.7, "all": 0.1},
    )
    assert result.answers["size"] == ScoreResult(
        1.25, 0.3, {"0": "a", "1": "b", "2": "c"}, {"0": 0.2, "1": 0.5, "2": 0.3}
    )


def test_transport_does_not_retry_on_its_own(http: Any) -> None:
    seen = http(lambda request: httpx2.Response(503, json={"error": "overloaded"}))
    with pytest.raises(JevTransportError) as caught:
        ask()
    assert len(seen) == 1
    assert caught.value.status == 503 and caught.value.retryable


def test_transport_marks_429_retryable_and_reads_retry_after(http: Any) -> None:
    http(lambda request: httpx2.Response(429, headers={"Retry-After": "3"}, json={"error": "slow"}))
    with pytest.raises(JevTransportError) as caught:
        ask()
    assert caught.value.retryable and caught.value.retry_after_s == 3.0


@pytest.mark.parametrize("status", [401, 403])
def test_transport_turns_a_rejected_key_into_an_auth_error(http: Any, status: int) -> None:
    http(lambda request: httpx2.Response(status, json={"error": "nope"}))
    with pytest.raises(AuthOrConfigError) as caught:
        ask()
    text = str(caught.value) + str(caught.value.hint)
    assert API_KEY_ENV in text and SECRET not in text


def test_transport_reads_max_tokens_exceeded_as_the_context_error(http: Any) -> None:
    http(
        lambda request: httpx2.Response(
            400, json={"error": {"code": "max_tokens_exceeded", "message": "too long"}}
        )
    )
    with pytest.raises(JevContextExceededError) as caught:
        ask()
    assert not caught.value.retryable and caught.value.status == 400


def test_transport_does_not_retry_another_400(http: Any) -> None:
    http(lambda request: httpx2.Response(400, json={"error": "bad question"}))
    with pytest.raises(JevTransportError, match="bad question") as caught:
        ask()
    assert not caught.value.retryable and not isinstance(caught.value, JevContextExceededError)


def test_transport_maps_a_timeout(http: Any) -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    http(slow)
    with pytest.raises(JevTimeoutError, match="timed out after 5 s"):
        ask()


def test_transport_maps_a_connection_failure(http: Any) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    http(down)
    with pytest.raises(JevTransportError, match="could not reach") as caught:
        ask()
    assert not caught.value.retryable and not isinstance(caught.value, JevTimeoutError)


def test_transport_errors_never_hold_the_request(http: Any) -> None:
    http(
        lambda request: httpx2.Response(
            422, json={"detail": [{"loc": ["body"], "msg": "bad", "input": "the diff"}]}
        )
    )
    with pytest.raises(JevTransportError) as caught:
        ask()
    assert "the diff" not in str(caught.value)


def test_transport_without_a_key_is_an_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with pytest.raises(AuthOrConfigError, match=API_KEY_ENV):
        jev_transport.TypeSafeTransport()


def test_an_end_to_end_run_through_the_sdk_transport(
    triage: JevSpec, trace: Trace, http: Any
) -> None:
    answers = {key: value for key, value in wire_answers().items() if key != "size"}
    body = {"model": MODEL, "answers": answers, "usage": {"input_tokens": 50, "output_tokens": 0}}
    http(lambda request: httpx2.Response(200, json=body))
    output = run_jev(triage, PAYLOAD, trace, sleep=Sleeps())
    assert output.model_dump()["reviewers"]["answer"] == "code-perf"
    (row,) = rows(trace)
    assert row["input_tokens"] == 50 and row["decision_json"]["model"] == MODEL


# --- the budget ----------------------------------------------------------------------------------

ONE_RUN_USD = cost_usd(MODEL, TokenUsage(input_tokens=120)) or 0.0


def test_a_run_is_charged_to_the_budget(triage: JevSpec, trace: Trace) -> None:
    budget = Budget(1.0)
    go(triage, FakeTransport(response()), trace, budget=budget)
    assert budget.spent == pytest.approx(ONE_RUN_USD)


def test_the_run_that_passes_the_budget_raises_after_its_row_is_written(
    triage: JevSpec, trace: Trace
) -> None:
    budget = Budget(ONE_RUN_USD * 1.5)
    go(triage, FakeTransport(response()), trace, budget=budget)
    with pytest.raises(BudgetExceeded):
        go(triage, FakeTransport(response()), trace, budget=budget)
    assert len(rows(trace)) == 2


def test_a_spent_budget_refuses_the_run_before_the_request(triage: JevSpec, trace: Trace) -> None:
    budget = Budget(0.5)
    budget.add(0.5)
    transport = FakeTransport(response())
    with pytest.raises(BudgetExceeded):
        go(triage, transport, trace, budget=budget)
    assert transport.calls == []
    assert rows(trace) == []


def test_a_failed_run_is_charged_and_keeps_its_own_error(triage: JevSpec, trace: Trace) -> None:
    budget = Budget(1.0)
    with pytest.raises(JevError):
        go(triage, FakeTransport(status_error(400)), trace, budget=budget)
    assert budget.spent == cost_usd(MODEL, TokenUsage())


def test_the_attempt_count_is_the_module_constant() -> None:
    assert MAX_ATTEMPTS == 3


# --- the trace tag -------------------------------------------------------------------------------


def _tags(trace: Trace) -> list[str | None]:
    trace.ensure_schema()
    return [row[0] for row in trace._connect().execute("SELECT tag FROM traces").fetchall()]


def test_the_tag_is_written_to_the_trace_row(triage: JevSpec, trace: Trace) -> None:
    go(triage, FakeTransport(response()), trace, tag="eval:run-1")
    assert _tags(trace) == ["eval:run-1"]


def test_a_run_without_a_tag_writes_none(triage: JevSpec, trace: Trace) -> None:
    go(triage, FakeTransport(response()), trace)
    assert _tags(trace) == [None]
