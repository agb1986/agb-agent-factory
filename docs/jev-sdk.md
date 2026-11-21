# Jev SDK spike

Findings of the one-day spike in #15, answering the two open questions in [PLAN.md](PLAN.md): how Python reaches Jev, and which model id to use. Every fact below was checked on 2026-10-25 against the source linked in its section.

No Jev key was available for the spike: `TYPESAFE_API_KEY` is unset and `agb sys secrets list` is empty. Nothing here was measured against a successful live call. The figures come from the vendor documentation, the public OpenAPI spec, the SDK source and OpenRouter's public listing. One live probe was possible without a key: [examples/jev_hello.py](../examples/jev_hello.py) run with a dummy key reaches `POST https://api.typesafe.ai/v1/systemone` and gets `401 Cannot authenticate with the server`, mapped to `TypeSafeAuthenticationError`, so the SDK, the endpoint and the request serialisation work end to end up to authentication. The checks still owed are listed under [Still to verify live](#still-to-verify-live).

## Go / no-go

Go: TypeSafe ships a stable-labelled, MIT-licensed Python SDK (`typesafe-sdk` 0.7.5) that installs next to `anthropic` 2.3.0 on the same `httpx2`, and the API is one POST that takes typed questions and returns probabilities. Input costs $0.042 per million tokens with output free, and the vendor's published latency for one call with 13 questions over a 54,000-character document is 0.27 s, so cost and speed fit a triage gate in front of the Claude panel. The decision is conditional on the first live run in #17 and flips to no-go if no key can be obtained, if `jev-1.14.0` is rejected, or if p95 latency for the triage questions over a 60,000-character diff exceeds 2 s.

## 1. Python access path

Checked 2026-10-25. Sources: [PyPI `typesafe-sdk`](https://pypi.org/project/typesafe-sdk/), [Python SDK docs](https://docs.typesafe.ai/sdk/python), [SDK changelog](https://docs.typesafe.ai/sdk/python/changelog), [SDK repository](https://github.com/typesafe-ai/typesafe-sdk-python), [OpenAPI spec](https://api.typesafe.ai/openapi.json), [OpenRouter endpoints for `typesafe/jev-1.14`](https://openrouter.ai/api/v1/models/typesafe/jev-1.14/endpoints).

The three candidates from the plan:

| Path | Verdict |
| --- | --- |
| Vendor SDK `typesafe-sdk` | **Use this.** Typed questions and answers, retries, typed errors, sync and async clients |
| Raw HTTP | Works and is the fallback if the SDK churns: `POST https://api.typesafe.ai/v1/systemone` and `GET /v1/models`, `Authorization: Bearer <key>`, OpenAPI 0.2.0 is public |
| OpenRouter | Not viable for typed questions. It lists `typesafe/jev-1.14` with output modality `decisions` and `supported_parameters: []`, and its chat API has no documented field for Noul, Choice or Score questions. A third-party blog claims it works; not verified |

The SDK:

- Install exactly `typesafe-sdk==0.7.5` (released 2026-10-14, "Production/Stable", MIT, Python >= 3.10). Pin the exact version: there were nine releases between 0.0.1a0 on 2026-09-09 and 0.7.5.
- Watch the name. `typesafe-ai` (0.1.0) is a shim, `typesafe` (0.9.1) is an unrelated project, and `jev` (0.3.0) is an unrelated third-party package.
- Dependencies: `anyio`, `httpx2>=2.0.0`, `packaging`, `pydantic>=2.12`, `tenacity`, `truststore`, `typing-extensions`; extras `http2`, `pil`, `zstd`. Checked locally: `typesafe-sdk` 0.7.5 and `anthropic` 2.3.0 resolve together on `httpx2` 2.16.0 with no `httpx` installed.
- `TypeSafeClient` and `AsyncTypeSafeClient` take `api_key`, `model`, `base_url`, `timeout`, `retry`, `headers`, `transport`, `http_client` and `compression` (zstd by default). They read `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL` and `TYPESAFE_LOG_LEVEL`.
- Defaults: base URL `https://api.typesafe.ai`, model `jev-latest`, timeout 10 s.
- Default retry policy: 2 retries, backoff from 0.5 s doubling to at most 5 s with 0.25 jitter, on 408, 429 and 5xx and on connection and timeout errors, honouring `retry-after` and `retry-after-ms`, within a 30 s total budget.
- Errors: `TypeSafeError` is the base. `TypeSafeAPIError` carries `status`, `body`, `headers`, `endpoint` and `request_id`, with subclasses for 400, 401, 403, 404, 422, 429 and 5xx. `TypeSafeAPIConnectionError` and `TypeSafeAPITimeoutError` cover the transport. A missing key raises `TypeSafeError` with a message that names `TYPESAFE_API_KEY`, never a value.
- Logging goes to the `typesafe_sdk` logger. At INFO it logs method, URL, status, latency in milliseconds and the `x-typesafe-request-id`. At DEBUG it also logs request and response bodies (truncated), with credential headers redacted but bodies not.

The Jev key is `TYPESAFE_API_KEY`, which confirms the name in [CONVENTIONS.md](CONVENTIONS.md#secrets).

## 2. Model id

Checked 2026-10-25. Sources: [Models](https://docs.typesafe.ai/models.md), [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md), [OpenRouter endpoints for `typesafe/jev-1.14`](https://openrouter.ai/api/v1/models/typesafe/jev-1.14/endpoints).

| Name | What it is | Kind |
| --- | --- | --- |
| `jev-1.14.0` | Newest release, available from 2026-10-14 | Pinned version |
| `jev-1.13.0` | Previous release, still served | Pinned version |
| `jev-latest` | Alias, currently points to `jev-1.14.0`; the SDK default | Rolling |
| `jev-preview` | Alias, currently points to `jev-1.14.0`; no preview build exists now | Rolling |
| `typesafe/jev-1.14-20261014` | OpenRouter's endpoint name for its model `typesafe/jev-1.14`, created 2026-10-14 | Not a TypeSafe API id |

Both names in the plan's open question are real, but only `jev-1.13.0` is a TypeSafe model id, and it is the older of the two current versions. `typesafe/jev-1.14-20261014` exists only on OpenRouter.

The factory pins `jev-1.14.0`. The vendor's own advice is that anyone with confidence thresholds tuned against one version sends that version's id rather than an alias, and the gates in #19 and #20 are exactly that. Every response has a `model` field with the versioned id that answered, which the trace should record.

`GET /v1/models` currently lists only the aliases, and the docs say versioned ids are accepted whether or not they are listed. The repository rule "verify any model id against `client.models.list()`" therefore cannot be met literally for a pinned Jev id. The live check is instead: `jev-latest` is in `client.models.list()`, and a call with `model="jev-1.14.0"` returns `response.model == "jev-1.14.0"`.

## 3. Wire format

Checked 2026-10-25. Sources: [OpenAPI spec](https://api.typesafe.ai/openapi.json), SDK 0.7.5 `typesafe_sdk/_schemas/models.py`, [Noul](https://docs.typesafe.ai/primitives/noul.md), [Choice](https://docs.typesafe.ai/primitives/choice.md), [Score](https://docs.typesafe.ai/primitives/score.md), [Confidence](https://docs.typesafe.ai/confidence.md).

Request to `POST /v1/systemone`:

```json
{
  "model": "jev-1.14.0",
  "state": "<diff text, or a JSON object or array>",
  "questions": {
    "needs_review": {"type": "noul", "instructions": "Does this diff need review?",
                     "criteria": {"true": "...", "false": "..."}},
    "reviewers": {"type": "choice", "instructions": "Which reviewers should see it?",
                  "criteria": {"code": "General code review", "code-security": null}},
    "risk": {"type": "score", "instructions": "How risky is this diff?",
             "criteria": ["Cosmetic", "Contained logic change", "Touches auth or data"]}
  }
}
```

- `questions` needs at least one entry. The question id is the key, and the model never sees it.
- `instructions` may be a string, an object or an array.
- Noul `criteria` is optional, with optional `true` and `false` descriptions.
- Choice `criteria` maps each label to a description or `null`, with 1 to 255 labels.
- Score `criteria` is an ordered list of level descriptions, lowest first, with 1 to 10 levels (the docs recommend at least two). A level's number is its 0-based position in the list.

Response:

```json
{
  "model": "jev-1.14.0",
  "answers": {
    "needs_review": {"type": "noul", "noul": 0.93},
    "reviewers": {"type": "choice", "choice": "code", "confidence": 0.7,
                  "probabilities": {"code": 0.85, "code-security": 0.15}},
    "risk": {"type": "score", "score": 1.4, "confidence": 0.25,
             "legend": {"0": "Cosmetic", "1": "Contained logic change", "2": "Touches auth or data"},
             "probabilities": {"0": 0.05, "1": 0.5, "2": 0.45}}
  },
  "usage": {"input_tokens": 3120, "output_tokens": 0}
}
```

The numbers are illustrative; the shapes are from the spec.

| Type | Answer fields | Confidence |
| --- | --- | --- |
| Noul | `noul`: probability of yes | **None.** `p` carries the uncertainty. The docs suggest 0.7 and 0.3 as starting thresholds, and `\|2p − 1\|` if one gate code must handle Nouls and Choices alike |
| Choice | `choice`: the most likely label; `probabilities`: label to probability | `confidence` = `(p_max − 1/n) / (1 − 1/n)` for `n` labels |
| Score | `score`: expected level (sum of level number times probability), may fall between levels; `probabilities` and `legend` keyed by level number as a string | `confidence` = `max(0, 1 − MAD / MAD_unif)`, where MAD is the probability-weighted distance in levels from the most likely level and MAD_unif is the same for an even spread, measured from the middle level |

A Score answer has no level name and no range. The most likely level is the argmax of `probabilities`, and its name comes from `legend`.

Other details:

- `usage.input_tokens` is billable; `output_tokens` is free.
- Error statuses: 400 for a rule break (`max_tokens_exceeded` when the request is over its context), 401 bad key, 403 no key (observed on `GET /v1/models` without a key), 413 body over 32 MiB, 415, 422 with `detail[{loc, msg, type, input, ctx}]`, 429 rate limit, 529 overloaded.
- `box` questions (image regions) exist but need image assets and the `TypeSafe-Danger-Zone-Images: true` header. The factory does not need them.

## 4. Batching

Checked 2026-10-25. Sources: [Models: context length](https://docs.typesafe.ai/models.md#context-length), [Parallel questions cookbook](https://docs.typesafe.ai/cookbooks/parallel_questions.md), [Speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md).

One request carries any number of questions over one `state`. There is no cap on the question count, only the context limits in section 5. Each question is scored independently against the state, so batching does not change answers. In the vendor's cookbook, 13 questions over a 54,000-character document gave identical answers batched or one per call, and the batched call was 12.2 times cheaper and 10 times faster than 13 sequential calls.

There is no endpoint that batches several states. Several diffs mean several requests, run concurrently through `AsyncTypeSafeClient` within the rate limits.

For the factory: a `kind: jev` agent sends all its questions in one request, which is how the [manifest](CONVENTIONS.md#manifest-kind-jev) is already shaped.

## 5. Limits

Checked 2026-10-25. Sources: [Models](https://docs.typesafe.ai/models.md), [Errors](https://docs.typesafe.ai/api/errors.md), [OpenAPI spec](https://api.typesafe.ai/openapi.json).

| Limit | Value |
| --- | --- |
| Context per request (state plus all questions) | 64k tokens |
| Context per question (state plus one question) | 32k tokens |
| Long context | `TypeSafe-Danger-Zone-Long-Context: true` raises both to 262k tokens at the same price, slower and less accurate |
| Request body | 32 MiB, UTF-8 JSON without BOM, nesting at most 64 levels, gzip or zstd allowed |
| Options per Choice | 1 to 255 labels |
| Levels per Score | 1 to 10 |
| Questions per request | No separate limit |
| Input | Text only for Jev 1.14 and 1.13; English is most accurate |
| Rate limits | Per organisation, in tokens per second and requests per second, shown in the [console](https://console.typesafe.ai/rate-limits); no public numbers. Over a limit returns 429, which the SDK retries |

Limits are in tokens, not characters, and TypeSafe publishes no tokenizer. The cookbook's batched call cost $0.000497 at $0.042 per million tokens, which is about 11,800 input tokens for 53,777 characters plus 13 questions, or about 4.5 characters per token. On that ratio, a 60,000-character input is about 13,000 tokens and fits the default 32k per-question limit; code diffs may tokenize less efficiently than prose, so `input.max_chars` should stay conservative until #17 measures real diffs. A request over its context returns 400 `max_tokens_exceeded`, which `jev_runner.py` should treat as truncation and force review.

## 6. Price

Checked 2026-10-25. Sources: [Models](https://docs.typesafe.ai/models.md), [OpenRouter endpoints for `typesafe/jev-1.14`](https://openrouter.ai/api/v1/models/typesafe/jev-1.14/endpoints).

| Model | Input | Output |
| --- | --- | --- |
| `jev-1.14.0` | $0.042 per million tokens ($42 per billion) | Free |
| `jev-1.13.0` | $0.042 per million tokens | Free |

OpenRouter lists the same price for `typesafe/jev-1.14` (`0.000000042` per prompt token, `0` per completion token). A triage call over a 13,000-token diff costs about $0.00055. `runtime/pricing.py` should carry this price with the date it was checked.

## 7. Latency at about 60,000 characters

Checked 2026-10-25. **Unverified: published by the vendor, not measured here, because no key was available.** Sources: [Parallel questions cookbook](https://docs.typesafe.ai/cookbooks/parallel_questions.md), [OpenRouter endpoints for `typesafe/jev-1.14`](https://openrouter.ai/api/v1/models/typesafe/jev-1.14/endpoints).

| Measurement | Model | Input | Questions | Latency |
| --- | --- | --- | --- | --- |
| One batched call, mean of 5 runs | `jev-1.12` | 53,777 characters | 13 (8 Noul, 2 Choice, 3 Score) | 0.27 s |
| 13 single calls, summed, mean of 5 runs | `jev-1.12` | 53,777 characters | 1 each | 2.71 s |

This is the closest published figure to 60,000 characters. It was measured by the vendor, on an older model, from an unknown location. OpenRouter reports no recent latency for `typesafe/jev-1.14` (`latency_last_30m` is null). The SDK's default 10 s timeout leaves ample headroom over the published figure.

The live measurement, once a key exists: 20 calls of the triage question set over a 60,000-character diff on `jev-1.14.0`, reporting p50 and p95 wall-clock time and the INFO log's server latency.

## Implications for #16 to #20

- **#16 `JevSpec`.** Set `model: jev-1.14.0`. Score answers carry `confidence`, so `confidence_below` is valid on scores as well as choices, and the score output model gains `confidence: float`. The API's Score has no range; `range` and `levels` in the manifest map onto `criteria` (levels, lowest first) and `level` is the argmax of `probabilities` named through `legend`. Choices may have up to 255 options, so the manifest's limit of 10 is a factory choice, not an API limit.
- **#17 `jev_runner.py`.** Use `typesafe-sdk==0.7.5` as the `[jev]` extra. Map `noul` to `p`, `choice` to `answer`, `probabilities` to `probs` and `score` to `value`. Record the response's `model` and `usage.input_tokens` in the trace. Never enable DEBUG on the `typesafe_sdk` logger outside local debugging, because it logs request and response bodies, which hold the diff. Map `TypeSafeAuthenticationError`, `TypeSafePermissionDeniedError` and a missing key to `AuthOrConfigError` (exit 4), and other SDK errors to `JevError`, which escalates with reason `jev_error`. Treat 400 `max_tokens_exceeded` as truncation, which forces review. Extend the `live` skip rule to `TYPESAFE_API_KEY`, and run the model id and latency checks below as live tests.
- **#18 triage, route-only.** Pack all triage questions into one request. The [Jev 1.13 jaggedness notes](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md) list two failure modes that matter here: a large state full of irrelevant detail lowers accuracy, and instructions injected into the state can move the answer. A diff can carry both, which is one more reason Jev stayed route-only until it was calibrated. Since `diff-triage@0.4.0` its skip acts on `factory review` only, and a 5% sample of skips still gets the full panel ([jev-calibration.md](jev-calibration.md#after-the-shadow-period)).
- **#19 judge gate and #20 calibration.** Pinning matters because thresholds are tuned per version; moving to a new Jev version re-runs calibration. The vendor's starting Noul thresholds (0.7 and 0.3) are a starting point only.

## Judge gate and grader agreement (#19)

Measured 2026-10-26 against the live API with `jev-1.14.0`, with `TYPESAFE_API_KEY` and `ANTHROPIC_API_KEY` set.

**Acceptance.** `tests/live/test_judge_gate_live.py` runs the review panel with `judge-gate@^0.1` twice, with the reviewers and the judge on a fake runner. On the `clean-refactor` fixture, with three approvals and no findings, the gate answered `clear_cut` 0.98 and `verdict` ship with confidence 0.98; the path was `skipped_judge`, no judge ran, and the estimated saving was $0.033. On the `mixed` fixture, with a blocker from one reviewer, it answered `clear_cut` 0.02 and `request_changes`; the path was `escalated`, reason `no_gate`, and the judge ran. The same file passes in 3.4 s.

**Gate behaviour on the eval cases.** Three runs of each case:

| Case | `clear_cut` | `verdict` |
| --- | --- | --- |
| Three approvals, no findings | 0.98 | ship, confidence 1.0 |
| One major finding, two approvals | 0.04 to 0.05 | request_changes, 1.0 |
| One blocker, two approvals | 0.01 | request_changes, 1.0 |
| Reviewers contradict each other | 0.01 to 0.02 | request_changes, 0.97 to 0.98 |
| One reviewer failed | 0.04 | unclear, 0.61 to 0.67 |

The gate never skips a change that has an open finding or a missing reviewer. It also never answers `unclear` for contradictory reviewers: it reads an unrefuted blocker as `request_changes` with high confidence. What keeps that case from skipping the judge is `clear_cut`, not `unclear`, so the eval case for it does not pin a verdict and states in `criteria` that the gate must not ship and must not be clear-cut. The case first expected `unclear` and failed on every run; the expectation was wrong about what this model does, and it was changed on that evidence.

**Threshold of the `jev` grader.** The grader asks one noul question, "does the answer meet the expectation?", and passes the answer above the `matches` gate of `evals/graders/jev_grader.yaml`. Three grader calls for each case, once with the answer the gate gave and once with a deliberately wrong answer:

| | Probability of `matches` |
| --- | --- |
| Right answers | 0.68 to 0.96 |
| Wrong answers | 0.01 to 0.02 |

The manifest started at 0.7, the vendor's suggested noul threshold. That sat inside the spread of right answers: with it, `factory eval judge-gate@^0.1` scored 2/5 and 3/5 on three runs, failing cases whose probability came out at 0.65 to 0.71. The threshold is now 0.5, the middle of the gap. After the change, five runs of the eval each passed 5/5 against the manifest's minimum of 0.9, at $0.00017 for the Jev calls of one run. The gap is wide because the grader is given the expectation and the answer, and not the task; a wrong answer is far from the expectation, and a borderline one has not been tested here.

**Agreement with the LLM judge.** `python -m evals.graders.jev judge-gate@^0.1 --json` grades each answer with both graders:

| Run | Compared | Agreed | Fell back |
| --- | --- | --- | --- |
| 1 | 5 | 3 | 0 |
| 2 | 5 | 3 | 0 |
| 3 | 5 | 4 | 0 |

In the run that printed reasons, the one disagreement was `split-panel`: the Jev grader passed it at 0.64, while the LLM judge failed it at 0.10, on the argument that a `clear_cut` of 0.01 "asserts the opposite of a split panel". That reading is backwards, since a low `clear_cut` is what a split panel should give. The LLM judge also passed `unanimous-blocker` with a score of 0.4. Every compared answer was a correct one, so the runs say how often the LLM judge fails a correct answer and nothing about how often either grader passes a wrong one; the table above is the only evidence for that. A full run costs about $0.001, of which the Jev grader is $0.00017. Only `judge-gate` was compared. The default run, over every agent, calls the real Claude agents with no budget, so it was not run; compare `code-reviewer@^1.2` the same way before moving an existing agent to `grader: jev`.

**Model listing.** `client.models.list()` returned only `jev-latest` and `jev-preview` on this date, and `jev-1.14.0` is not in it. The pinned id still answers real calls, which is what the judge gate's live test relies on, so that test does not assert membership of the listing. `tests/live/test_jev_live.py` and `tests/live/test_triage_live.py` asserted it and failed on it; #20 changed both to the check in [Model id](#2-model-id): `jev-latest` is listed, and the `model` the response returns, as recorded in the Jev run's trace row, equals the pinned id.

## Still to verify live

These need `TYPESAFE_API_KEY` and belong in #17's live tests:

1. `client.models.list()` returns `jev-latest`, and a call with `model="jev-1.14.0"` returns `response.model == "jev-1.14.0"`.
2. `examples/jev_hello.py` prints `p` for one Noul.
3. p50 and p95 latency for the triage question set over a 60,000-character diff, against the 2 s no-go line.
4. Input tokens for a real 60,000-character diff, to set `input.max_chars`.
5. The organisation's rate limits in the console.

## Running the example

```bash
export TYPESAFE_API_KEY=...   # from your shell or `agb sys secrets`, never committed
uv run examples/jev_hello.py
```

The example declares `typesafe-sdk==0.7.5` as [PEP 723](https://peps.python.org/pep-0723/) inline script metadata, so `uv run` installs it in an isolated environment without touching `pyproject.toml`.
