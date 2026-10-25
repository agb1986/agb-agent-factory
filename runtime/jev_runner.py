"""The Jev runner: one `kind: jev` agent run, from a payload to a validated `JevOutput`.

`run_jev` renders `input.render` with the payload, cuts the text to `input.max_chars`, sends every
question of the manifest in one request, and parses the answers into `spec.output_class`. All
knowledge of the vendor sits behind the one-method `JevTransport` protocol; the only implementation
is `runtime.jev_transport`, the one module that imports `typesafe_sdk`. That module is imported when
a run needs it, not when this one is, so the `[jev]` extra is optional until a Jev agent runs.

Truncation. A rendered text longer than `max_chars` is cut there and a marker line is appended, and
the trace row's `decision_json` says `truncated: true`. A request the API rejects as over its
context (400 `max_tokens_exceeded`) is also truncation: the text is cut to half its length, the
request is sent again, and the run counts as truncated. This repeats until the request fits or the
text is down to `MIN_CHARS`, which ends the run with a `JevError`. A truncated input forces review
whatever the gates say (see "Gates" in docs/CONVENTIONS.md).

Failure. A request is sent up to `MAX_ATTEMPTS` times. Only a 429 or a 5xx is sent again, after an
exponential backoff of `BACKOFF_S` seconds, doubled each time, or the server's `Retry-After` when it
gives one. The transport sets `TIMEOUT_S` per request and does not retry itself, so attempts do not
multiply. A timeout, an exhausted retry, a malformed answer or any other API error is a `JevError`,
which a pipeline escalates with reason `jev_error`. A missing or rejected key and a missing
`typesafe-sdk` are an `AuthOrConfigError` (exit 4), raised when a run starts, not on import.

Answer mapping, per question type (wire name on the left, output field on the right):

- noul: `noul` becomes `p`.
- choice: `choice` becomes `answer`, `probabilities` becomes `probs`, `confidence` is kept.
- score: the API sends `score`, `probabilities` and `legend`, both keyed by level number as a
  string ("0" is the lowest level). The manifest's `levels` name those numbers by position, and the
  runner sends them as the question's criteria in that order. `probs` is keyed by level name.
  `level` is the name of the likeliest level; a tie goes to the lower level. `confidence` is kept.

Score `value` and `range`. The API's `score` is the probability-weighted level number, a float
from 0 to n-1 for n levels, and it falls between levels when the probability is spread. The
manifest's `range` is `[min, max]`. `value` is the score mapped linearly onto the range, with the
lowest level at `min` and the highest at `max`:

    value = min + score / (n - 1) * (max - min)

So `value` stays inside `range` and keeps the API's meaning of an expected level. A score the API
sends outside 0..n-1 is a malformed answer and a `JevError`, not clamped. A range of `[0, 100]` on
three levels reads 0, 50 and 100 at the levels, and 75 for an expected level of 1.5.

Trace. Exactly one row is written for every run whose payload is valid, on success or failure:
`kind="jev"`, `agent_ref` the `name@version` address, `input_tokens` from the response's usage,
`output_tokens` 0 (output is free), `cost_usd` from `runtime.pricing`, `output_json` the answers
with their full probability vectors, and `decision_json` `{"truncated": bool, "model": <the model
the response named>}`. A failed run has `error` as `<ErrorClass>: <message>`.

Nothing here logs the rendered text or the payload, and none of the errors repeat them.
"""

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from factory.exit import AuthOrConfigError, FactoryError, UsageError
from registry.jev_spec import ChoiceQuestion, JevOutput, JevSpec, NoulQuestion, ScoreQuestion
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace, TraceRow

API_KEY_ENV = "TYPESAFE_API_KEY"
"""The environment variable the default transport reads the API key from."""

TIMEOUT_S = 5.0
"""Seconds one request may take before it is a timeout."""

MAX_ATTEMPTS = 3
"""How many times a request is sent when the API answers 429 or 5xx."""

BACKOFF_S = 0.5
"""Seconds to wait before the second attempt; each later wait doubles."""

MAX_BACKOFF_S = 5.0
"""The longest the runner waits between attempts, `Retry-After` included."""

MIN_CHARS = 1_000
"""The shortest text a `max_tokens_exceeded` retry cuts down to before the run fails."""

KEY_HINT = f"store it with `agb sys secrets set {API_KEY_ENV}` and export it"
EXTRA_HINT = "install the extra with `uv sync --extra jev`"


class JevError(FactoryError):
    """A Jev run failed (exit 1). A pipeline escalates with reason `jev_error`."""


class JevTransportError(Exception):
    """A request failed in the transport.

    `status` is the HTTP status, or `None` when there was none. `retryable` is true for the
    failures the runner sends again (429 and 5xx), and `retry_after_s` is the delay the server
    asked for, if it did. The message never holds the request or response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after_s = retry_after_s


class JevTimeoutError(JevTransportError):
    """A request ran past its timeout. It is not sent again."""


class JevContextExceededError(JevTransportError):
    """The API rejected the request as over its context (400 `max_tokens_exceeded`)."""


@dataclass(frozen=True, slots=True)
class JevQuestion:
    """One question in a request. `labels` are a choice's options or a score's levels, in order."""

    id: str
    kind: Literal["noul", "choice", "score"]
    text: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NoulResult:
    """A noul answer as the API sent it."""

    noul: float


@dataclass(frozen=True, slots=True)
class ChoiceResult:
    """A choice answer as the API sent it."""

    choice: str
    confidence: float
    probabilities: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """A score answer as the API sent it. `legend` and `probabilities` are keyed by level number."""

    score: float
    confidence: float
    legend: Mapping[str, Any]
    probabilities: Mapping[str, float]


type JevResult = NoulResult | ChoiceResult | ScoreResult


@dataclass(frozen=True, slots=True)
class JevResponse:
    """What one request returned: the model that answered, billable input tokens, the answers."""

    model: str
    input_tokens: int | None
    answers: Mapping[str, JevResult]


class JevTransport(Protocol):
    """How a run reaches Jev. One method; no vendor type crosses it."""

    def ask(
        self, *, model: str, state: str, questions: Sequence[JevQuestion], timeout_s: float
    ) -> JevResponse:
        """Send `questions` about `state` to `model` in one request and return the answers.

        Raises `JevTransportError` (or a subclass) for a failed request and `AuthOrConfigError`
        for a key the API rejects.
        """
        ...


def default_transport() -> JevTransport:
    """Build the transport on the TypeSafe SDK.

    Raises `AuthOrConfigError` when the `typesafe-sdk` extra is not installed or `TYPESAFE_API_KEY`
    is not set. The hint names what to do and never a value.
    """
    try:
        from runtime.jev_transport import TypeSafeTransport
    except ModuleNotFoundError as exc:
        if exc.name != "typesafe_sdk":
            raise
        raise AuthOrConfigError(
            "the typesafe-sdk package is not installed", hint=EXTRA_HINT
        ) from exc
    return TypeSafeTransport()


def run_jev(
    spec: JevSpec,
    payload: Mapping[str, Any] | BaseModel,
    trace: Trace,
    *,
    pipeline_run_id: str | None = None,
    transport: JevTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JevOutput:
    """Run the Jev agent `spec` on `payload` and return an instance of its output model.

    `spec` is a resolved `JevSpec`. `payload` is a dict or a model; it must have every field
    `input.render` uses, and a missing one raises `UsageError` before anything is sent or traced.
    `transport` defaults to the SDK transport (see `default_transport`); `sleep` waits out a
    backoff and is a parameter so tests need not wait. `pipeline_run_id` lands in the trace row.

    Past the payload check, exactly one row is written to `trace`, whether the run succeeds or
    fails. Raises `JevError` for a failed run and `AuthOrConfigError` for a missing or rejected key
    or a missing `typesafe-sdk`.
    """
    payload_json = _payload_json(spec, payload)
    text = spec.input.render.format(**payload_json)
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    start = time.monotonic()
    state = _RunState()
    output: JevOutput | None = None
    error: BaseException | None = None
    try:
        output = _run(spec, text, state, transport, sleep)
        return output
    except BaseException as exc:
        error = exc
        raise
    finally:
        trace.record(
            TraceRow(
                run_id=run_id,
                agent_ref=spec.address,
                kind="jev",
                started_at=started_at,
                duration_ms=int((time.monotonic() - start) * 1000),
                input_json=payload_json,
                pipeline_run_id=pipeline_run_id,
                input_tokens=state.input_tokens,
                output_tokens=0,
                cost_usd=cost_usd(state.model or spec.model, TokenUsage(state.input_tokens, 0)),
                decision_json={"truncated": state.truncated, "model": state.model},
                output_json=None if output is None else output.model_dump(mode="json"),
                error=None if error is None else f"{type(error).__name__}: {error}",
            )
        )


@dataclass
class _RunState:
    """What a run learns on the way, kept for the trace row even when the run fails."""

    truncated: bool = False
    model: str | None = None
    input_tokens: int | None = None


def _payload_json(spec: JevSpec, payload: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """Return `payload` as JSON-ready data, checked for the fields `input.render` uses.

    Raises `UsageError` naming the missing fields, never their values.
    """
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    missing = [name for name in spec.input.fields if name not in data]
    if missing:
        raise UsageError(
            f"payload for {spec.address} is missing {', '.join(missing)}",
            hint=f"input.render uses {', '.join(spec.input.fields)}",
        )
    try:
        json.dumps(data)
    except (TypeError, ValueError) as exc:
        raise UsageError(f"payload for {spec.address} is not JSON-serializable: {exc}") from exc
    return data


def _run(
    spec: JevSpec,
    text: str,
    state: _RunState,
    transport: JevTransport | None,
    sleep: Callable[[float], None],
) -> JevOutput:
    transport = transport if transport is not None else default_transport()
    questions = _questions(spec)
    limit = spec.input.max_chars
    while True:
        state_text = _truncate(text, limit)
        state.truncated = state.truncated or state_text != text
        try:
            response = _send(spec, transport, state_text, questions, sleep)
        except JevContextExceededError as exc:
            limit = min(limit, len(text)) // 2
            state.truncated = True
            if limit < MIN_CHARS:
                raise JevError(
                    f"{spec.address} is over the Jev context even with the input cut to "
                    f"{MIN_CHARS} characters"
                ) from exc
            continue
        state.model = response.model
        state.input_tokens = response.input_tokens
        return _parse(spec, response)


def _truncate(text: str, limit: int) -> str:
    """Return `text` cut to `limit` characters with a marker line, or unchanged if it fits."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated: input cut to {limit} of {len(text)} characters]"


def _send(
    spec: JevSpec,
    transport: JevTransport,
    text: str,
    questions: Sequence[JevQuestion],
    sleep: Callable[[float], None],
) -> JevResponse:
    """Send the request, again after a backoff on 429 or 5xx, up to `MAX_ATTEMPTS` times."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return transport.ask(
                model=spec.model, state=text, questions=questions, timeout_s=TIMEOUT_S
            )
        except JevContextExceededError:
            raise
        except JevTransportError as exc:
            if not exc.retryable or attempt == MAX_ATTEMPTS:
                reason = f"gave up after {attempt} attempts: " if exc.retryable else ""
                raise JevError(f"{spec.address}: {reason}{exc}") from exc
            sleep(_delay(attempt, exc.retry_after_s))
    raise AssertionError("unreachable")  # pragma: no cover


def _delay(attempt: int, retry_after_s: float | None) -> float:
    """Seconds to wait after the failed `attempt`: the server's ask, else doubling from `BACKOFF_S`."""
    delay = retry_after_s if retry_after_s is not None else BACKOFF_S * 2 ** (attempt - 1)
    return min(delay, MAX_BACKOFF_S)


def _questions(spec: JevSpec) -> tuple[JevQuestion, ...]:
    questions = []
    for question in spec.questions:
        match question:
            case NoulQuestion():
                questions.append(JevQuestion(question.id, "noul", question.text))
            case ChoiceQuestion():
                questions.append(
                    JevQuestion(question.id, "choice", question.text, question.options)
                )
            case ScoreQuestion():
                questions.append(JevQuestion(question.id, "score", question.text, question.levels))
    return tuple(questions)


def _parse(spec: JevSpec, response: JevResponse) -> JevOutput:
    """Map the API's answers onto the output model, or raise `JevError` if they do not fit."""
    answers: dict[str, dict[str, Any]] = {}
    for question in spec.questions:
        result = response.answers.get(question.id)
        try:
            answers[question.id] = _answer(question, result)
        except _Malformed as exc:
            raise JevError(f"{spec.address}: answer {question.id!r} is malformed: {exc}") from exc
    try:
        return spec.output_class.model_validate(answers)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise JevError(
            f"{spec.address}: answers do not match the output model: {problems}"
        ) from exc


class _Malformed(Exception):
    """An answer is missing, of the wrong type, or inconsistent with its question."""


def _answer(
    question: NoulQuestion | ChoiceQuestion | ScoreQuestion, result: JevResult | None
) -> dict[str, Any]:
    match question, result:
        case NoulQuestion(), NoulResult():
            return {"p": result.noul}
        case ChoiceQuestion(), ChoiceResult():
            return {
                "answer": result.choice,
                "probs": dict(result.probabilities),
                "confidence": result.confidence,
            }
        case ScoreQuestion(), ScoreResult():
            return _score_answer(question, result)
        case _, None:
            raise _Malformed("the response has no answer for it")
        case _:
            raise _Malformed(f"expected a {question.type} answer, got {type(result).__name__}")


def _score_answer(question: ScoreQuestion, result: ScoreResult) -> dict[str, Any]:
    """Map a score answer onto `value`, `level`, `probs` and `confidence`; see the module docstring."""
    levels = question.levels
    keys = [str(index) for index in range(len(levels))]
    if set(result.probabilities) != set(keys) or set(result.legend) != set(keys):
        raise _Malformed(f"probabilities and legend must be keyed {keys[0]} to {keys[-1]}")
    top = len(levels) - 1
    if not 0 <= result.score <= top:
        raise _Malformed(f"score is outside 0 to {top}")
    low, high = question.range
    # max() returns the first of equal keys, so a tie goes to the lower level.
    likeliest = max(keys, key=lambda key: result.probabilities[key])
    return {
        "value": low + result.score / top * (high - low),
        "level": levels[int(likeliest)],
        "probs": {levels[int(key)]: result.probabilities[key] for key in keys},
        "confidence": result.confidence,
    }
