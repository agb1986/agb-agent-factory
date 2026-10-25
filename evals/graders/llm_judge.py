"""The `llm_judge` grader: a small model decides whether an answer matches the expectation.

One structured-output call to `JUDGE_MODEL` sees the case's expectation and the agent's answer and
returns `pass`, a `score` from 0 to 1 and a `reason`. The call follows the request rules in
"Request fields" in docs/CONVENTIONS.md (no sampling parameters, structured output through
`output_config.format`). An answer that fails validation is retried once with the errors, then
raises `JudgeError`. Every call writes one trace row under the eval's tag.
"""

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.cases import Expected
from factory.exit import AuthOrConfigError, FactoryError
from runtime.pricing import TokenUsage, cost_usd
from runtime.trace import Trace, TraceRow

JUDGE_MODEL = "claude-haiku-5-5"
"""The model that grades. Configuration: change it here, and its price in `runtime.pricing`."""

JUDGE_MAX_TOKENS = 1024
JUDGE_TIMEOUT_S = 60
JUDGE_REF = f"eval-judge@{JUDGE_MODEL}"
"""The `agent_ref` the judge's trace rows carry."""

API_KEY_ENV = "ANTHROPIC_API_KEY"

SYSTEM_PROMPT = """\
You grade the answer of a code-review agent against what a good answer must contain.

You get a JSON object with `expected` (the verdict, and the defects a good review reports, each \
with a file, lines and a minimum severity) and `actual` (the agent's verdict, findings and \
summary). The verdict and the presence of each expected defect were already checked mechanically, \
so judge substance instead:

- Does each finding that matches an expected defect really describe that defect, rather than \
mention the same lines for an unrelated reason?
- Is the verdict justified by the findings and the summary?
- Are the extra findings, if any, plausible and not noise that would mislead a reader? Extra \
findings are acceptable unless they are plainly wrong or contradict the verdict.

Some agents judge a panel of reviews instead of reviewing. Their answer has `scores`, \
`merged_findings` and a `rationale` instead of a verdict, findings and summary, and `expected` \
then has no verdict. Read `merged_findings` as the findings and the rationale as the summary, and \
check that the scores agree with them.

When `expected` has `criteria`, the answer passes only if it meets every one of them.

Set `pass` to true only when the answer is a good review for the expectation. Set `score` between \
0 and 1: 1 for a precise, complete review, 0 for one that misses the point. Give a one or two \
sentence `reason` that names what decided it.\
"""


class JudgeError(FactoryError):
    """The judge could not give a valid verdict: a bad answer twice, a refusal, or an API failure."""


class JudgeVerdict(BaseModel):
    """What the judge returns. `pass` is a Python keyword, so the field is `passed` with that alias."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    passed: Annotated[bool, Field(alias="pass")]
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    reason: str


type Judge = Callable[[Expected, Mapping[str, Any]], JudgeVerdict]
"""A grader for the answers that pass the deterministic checks; tests inject their own."""


class LLMJudge:
    """The real judge: calls `JUDGE_MODEL` and traces every call.

    The client is built on first use from `ANTHROPIC_API_KEY` (a missing key raises
    `AuthOrConfigError`) unless one is passed in. It is safe to call from several threads.
    `close()` closes a client this object built.
    """

    def __init__(
        self,
        trace: Trace,
        *,
        tag: str | None = None,
        pipeline_run_id: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._trace = trace
        self._tag = tag
        self._pipeline_run_id = pipeline_run_id
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()

    def close(self) -> None:
        """Close the client this judge built, if it built one."""
        with self._lock:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None

    def __call__(self, expected: Expected, actual: Mapping[str, Any]) -> JudgeVerdict:
        """Return the judge's verdict on `actual`, tracing the call whether it succeeds or not."""
        payload = {
            "expected": expected.model_dump(mode="json", exclude_none=True),
            "actual": dict(actual),
        }
        started_at = datetime.now(UTC)
        start = time.monotonic()
        usage = TokenUsage(0, 0, 0, 0)
        verdict: JudgeVerdict | None = None
        error: BaseException | None = None
        try:
            verdict, usage = self._judge(payload)
            return verdict
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._trace.record(
                TraceRow(
                    run_id=str(uuid.uuid4()),
                    agent_ref=JUDGE_REF,
                    kind="llm",
                    started_at=started_at,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    input_json=payload,
                    pipeline_run_id=self._pipeline_run_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost_usd=cost_usd(JUDGE_MODEL, usage),
                    output_json=None if verdict is None else verdict.model_dump(mode="json"),
                    error=None if error is None else f"{type(error).__name__}: {error}",
                    tag=self._tag,
                )
            )

    def _get_client(self) -> anthropic.Anthropic:
        with self._lock:
            if self._client is None:
                if not os.environ.get(API_KEY_ENV):
                    raise AuthOrConfigError(
                        f"{API_KEY_ENV} is not set",
                        hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
                    )
                self._client = anthropic.Anthropic()
            return self._client

    def _judge(self, payload: dict[str, Any]) -> tuple[JudgeVerdict, TokenUsage]:
        client = self._get_client()
        messages: list[dict[str, Any]] = [{"role": "user", "content": json.dumps(payload)}]
        usage = TokenUsage(0, 0, 0, 0)
        for attempt in (1, 2):
            text, call_usage = self._ask(client, messages)
            usage = TokenUsage(
                (usage.input_tokens or 0) + (call_usage.input_tokens or 0),
                (usage.output_tokens or 0) + (call_usage.output_tokens or 0),
                (usage.cache_read_tokens or 0) + (call_usage.cache_read_tokens or 0),
                (usage.cache_write_tokens or 0) + (call_usage.cache_write_tokens or 0),
            )
            try:
                return JudgeVerdict.model_validate_json(text), usage
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(part) for part in err['loc']) or '(root)'}: {err['msg']}"
                    for err in exc.errors(include_url=False)
                )
                if attempt == 2:
                    raise JudgeError(
                        f"the judge answered with an invalid verdict twice: {problems}"
                    ) from exc
                messages += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            f"Your answer does not validate: {problems}. "
                            "Reply again with only the corrected JSON object."
                        ),
                    },
                ]
        raise AssertionError("unreachable")  # pragma: no cover

    def _ask(
        self, client: anthropic.Anthropic, messages: list[dict[str, Any]]
    ) -> tuple[str, TokenUsage]:
        try:
            message = client.messages.create(**build_request(messages), timeout=JUDGE_TIMEOUT_S)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AuthOrConfigError(
                f"the Messages API refused the key in {API_KEY_ENV} ({exc.status_code})",
                hint=f"store it with `agb sys secrets set {API_KEY_ENV}` and export it",
            ) from exc
        except anthropic.APIError as exc:
            raise JudgeError(
                f"the Messages API failed for the judge: {type(exc).__name__}: {exc.message}"
            ) from exc
        usage = TokenUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens or 0,
            cache_write_tokens=message.usage.cache_creation_input_tokens or 0,
        )
        if message.stop_reason == "refusal":
            raise JudgeError("the judge refused to grade this answer")
        if message.stop_reason != "end_turn":
            raise JudgeError(f"the judge stopped with {message.stop_reason!r} before it answered")
        text = "".join(block.text for block in message.content if block.type == "text")
        return text, usage


def build_request(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the keyword arguments for the judge's `messages.create` call.

    Sets the model, `max_tokens`, the system prompt, and `output_config.format` with the verdict's
    JSON schema. Sets no sampling parameter and no forced tool choice.
    """
    return {
        "model": JUDGE_MODEL,
        "max_tokens": JUDGE_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [dict(message) for message in messages],
        "output_config": {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(JudgeVerdict)}
        },
    }
