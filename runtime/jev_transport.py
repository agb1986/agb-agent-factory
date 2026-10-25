"""The TypeSafe SDK transport for `runtime.jev_runner`.

This is the one module that imports `typesafe_sdk`, which the optional `[jev]` extra installs. The
runner imports it when a Jev run starts, so a checkout without the extra still imports everything
else. Nothing from the SDK leaves this module: questions go in as `JevQuestion`, answers come out
as the plain dataclasses of `jev_runner`, and every SDK error is turned into a `JevTransportError`
(or a subclass) or an `AuthOrConfigError`.

The SDK's own retry is switched off (`RetryPolicy(max_retries=0)`) because the runner retries, with
a backoff it controls and tests can see. Letting both retry would multiply the attempts.

Never turn on DEBUG for the `typesafe_sdk` logger: it logs request and response bodies, and the
request body is the diff under review. This module logs nothing, and its error messages hold the
status, the request id and the API's own message for a 400 or 422, never the request.
"""

import os
from collections.abc import Sequence
from typing import Any

from typesafe_sdk import (
    Choice,
    Noul,
    Question,
    RetryPolicy,
    Score,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafePermissionDeniedError,
)
from typesafe_sdk._core.errors import extract_message

from factory.exit import AuthOrConfigError
from runtime.jev_runner import (
    API_KEY_ENV,
    KEY_HINT,
    TIMEOUT_S,
    ChoiceResult,
    JevContextExceededError,
    JevQuestion,
    JevResponse,
    JevResult,
    JevTimeoutError,
    JevTransportError,
    NoulResult,
    ScoreResult,
)

_NO_RETRY = RetryPolicy(max_retries=0)
_CONTEXT_EXCEEDED = "max_tokens_exceeded"
_MESSAGE_CHARS = 300


class TypeSafeTransport:
    """Sends one batched request to the TypeSafe API through the SDK client."""

    def __init__(self) -> None:
        if not os.environ.get(API_KEY_ENV):
            raise AuthOrConfigError(f"{API_KEY_ENV} is not set", hint=KEY_HINT)

    def ask(
        self, *, model: str, state: str, questions: Sequence[JevQuestion], timeout_s: float
    ) -> JevResponse:
        """Send `questions` about `state` to `model` in one request; see `JevTransport.ask`."""
        try:
            with TypeSafeClient(model=model, timeout=timeout_s, retry=_NO_RETRY) as client:
                response = client.system_one(
                    state=state,
                    questions={question.id: _question(question) for question in questions},
                    model=model,
                    timeout=timeout_s,
                    retry=_NO_RETRY,
                )
        except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
            raise AuthOrConfigError(
                f"the Jev API rejected {API_KEY_ENV} ({_describe(exc)})", hint=KEY_HINT
            ) from exc
        except TypeSafeAPITimeoutError as exc:
            raise JevTimeoutError(f"the request timed out after {timeout_s:g} s") from exc
        except TypeSafeAPIConnectionError as exc:
            raise JevTransportError("could not reach the Jev API") from exc
        except TypeSafeAPIError as exc:
            raise _api_error(exc) from exc
        except TypeSafeError as exc:
            raise JevTransportError(f"{type(exc).__name__}: {_clip(str(exc))}") from exc
        answers: dict[str, JevResult] = {}
        for name, answer in response.nouls.items():
            answers[name] = NoulResult(noul=answer.noul)
        for name, answer in response.choices.items():
            answers[name] = ChoiceResult(
                choice=answer.choice,
                confidence=answer.confidence,
                probabilities=dict(answer.probabilities),
            )
        for name, answer in response.scores.items():
            answers[name] = ScoreResult(
                score=answer.score,
                confidence=answer.confidence,
                legend={str(level): text for level, text in answer.legend.items()},
                probabilities={str(level): p for level, p in answer.probabilities.items()},
            )
        return JevResponse(
            model=response.model, input_tokens=response.usage.input_tokens, answers=answers
        )


def available_models() -> frozenset[str]:
    """Return the model names and aliases the account can call, for the live test of `PRICES`."""
    if not os.environ.get(API_KEY_ENV):
        raise AuthOrConfigError(f"{API_KEY_ENV} is not set", hint=KEY_HINT)
    try:
        with TypeSafeClient(timeout=TIMEOUT_S, retry=_NO_RETRY) as client:
            return frozenset(model.name for model in client.models.list().models)
    except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
        raise AuthOrConfigError(
            f"the Jev API rejected {API_KEY_ENV} ({_describe(exc)})", hint=KEY_HINT
        ) from exc
    except TypeSafeError as exc:
        raise JevTransportError(f"could not list the Jev models: {type(exc).__name__}") from exc


def _question(question: JevQuestion) -> Question:
    match question.kind:
        case "noul":
            return Noul(instructions=question.text)
        case "choice":
            return Choice(
                instructions=question.text, criteria={label: None for label in question.labels}
            )
        case "score":
            return Score(instructions=question.text, criteria=list(question.labels))


def _api_error(exc: TypeSafeAPIError) -> JevTransportError:
    """Turn an SDK API error into the runner's error: 429 and 5xx are retryable."""
    status = exc.status
    if status == 400 and _CONTEXT_EXCEEDED in _body_text(exc.body):
        return JevContextExceededError(_describe(exc), status=status)
    retry_after_ms = getattr(exc, "retry_after_ms", None)
    return JevTransportError(
        _describe(exc),
        status=status,
        retryable=status == 429 or status >= 500,
        retry_after_s=None if retry_after_ms is None else retry_after_ms / 1000,
    )


def _describe(exc: TypeSafeAPIError | TypeSafeError) -> str:
    """Return the status, request id and, for a 400 or 422, the API's message; never the request."""
    if not isinstance(exc, TypeSafeAPIError):
        return type(exc).__name__
    parts = [f"HTTP {exc.status}"]
    if exc.request_id:
        parts.append(f"request id {exc.request_id}")
    if exc.status in (400, 422):
        message = extract_message(exc.body)
        if message:
            parts.append(_clip(message))
    return ", ".join(parts)


def _body_text(body: Any) -> str:
    return body if isinstance(body, str) else repr(body)


def _clip(text: str) -> str:
    return text if len(text) <= _MESSAGE_CHARS else f"{text[:_MESSAGE_CHARS]}..."
