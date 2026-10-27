# Conventions

These are the rules every agent, runtime module, pipeline and command in this repository follows. They were fixed in phase 1 (#1), before the first agent was written. The reasons behind them are in [PLAN.md](PLAN.md).

Each section states one rule and gives a minimal example. A ticket that has to invent a new convention adds one short paragraph under the right heading in the same pull request. A ticket that changes a convention updates its section here in that pull request too.

| Convention | Section | Enforced in |
| --- | --- | --- |
| Repository layout | [Repository layout](#repository-layout) | `registry/resolver.py` (#4) |
| `kind: llm` manifest | [Manifest: kind llm](#manifest-kind-llm) | `registry/spec.py` (#4) |
| `kind: jev` manifest | [Manifest: kind jev](#manifest-kind-jev) | `registry/jev_spec.py` (#16) |
| Agent address and versions | [Agent address](#agent-address) | `registry/resolver.py` (#4) |
| Output contract | [Output contract](#output-contract) | `runtime/runner.py` (#5) |
| Request fields | [Request fields](#request-fields) | `runtime/runner.py` (#5) |
| Async runtime | [Async runtime](#async-runtime) | `runtime/async_runner.py`, `runtime/trace.py` (#26) |
| Tool naming, allow and deny | [Tools](#tools) | `tools/registry.py` (#3), `runtime/runner.py` (#5) |
| Fixture repositories | [Fixtures](#fixtures) | `fixtures/build.py`, `fixtures/schema.py` (#9) |
| Gates and routing | [Gates](#gates) | `runtime/gates.py`, `pipelines/triage.py` (#18) |
| Trace row | [Trace row](#trace-row) | `runtime/trace.py` (#6) |
| Secrets | [Secrets](#secrets) | `runtime/runner.py` (#5), `runtime/jev_runner.py` (#17) |
| Deployment image | [Deployment](#deployment) | `Dockerfile`, `compose.yaml`, `scripts/scan_image.py` (#28) |
| CLI shape | [CLI shape](#cli-shape) | `factory/cli.py` |
| Exit codes and errors | [Exit codes](#exit-codes), [Errors](#errors) | `factory/exit.py` |

## Repository layout

| Path | Purpose |
| --- | --- |
| `agents/<name>/` | One agent: `agent.yaml`, `system.md` (LLM only), `schemas.py` (LLM only), `evals/cases.yaml` |
| `registry/` | Manifest loading, semver resolution, `LLMSpec` and `JevSpec` |
| `runtime/` | `runner.py` (LLM) with `hooks.py` and `errors.py`, `jev_runner.py` (Jev), `gates.py`, `trace.py`, `pricing.py` |
| `tools/` | Registry of scoped tool implementations, read-only by default |
| `pipelines/` | `review_panel.py`, `fix_loop.py` |
| `evals/` | Eval runner and calibration data |
| `fixtures/` | Scripts that build small fixture repositories with planted defects |
| `factory/` | The `factory` command (`cli.py`) and the exit codes and error types (`exit.py`) |
| `docs/` | Conventions, plan, Jev spike notes, decision records in `docs/decisions/` |
| `prototypes/` | Throwaway evidence for a decision record, one subdirectory per question |

An agent directory is named after the agent: lowercase words joined by hyphens, equal to the manifest's `name`. To keep several versions side by side, put each in a subdirectory named after its full version. That subdirectory name must equal the manifest's `version`. Both layouts may be used for the same agent, and the resolver considers all of them.

```text
agents/code-reviewer/agent.yaml              # one version, here 1.2.0
agents/code-reviewer/system.md
agents/code-reviewer/schemas.py
agents/code-reviewer/evals/cases.yaml
agents/code-reviewer/1.3.0/agent.yaml        # a second version beside it
```

Models that several agents share live in `registry/common_schemas.py`. An agent's `schemas.py` is imported by file path, so it cannot import a sibling agent's module; a reviewer that uses the shared review contract (`ReviewInput`, `Finding`, `ReviewOutput`, `Severity`, `Verdict`) imports it from `registry.common_schemas` and re-exports the names its manifest uses. Moving a model there without changing its JSON schema is not a version bump.

A decision record is `docs/decisions/NNNN-<slug>.md` with the sections Context, Options considered, Decision and Consequences. The code it cites as evidence lives in `prototypes/<question>/`, which production code never imports and the default `uv run pytest` does not collect; run it with `uv run pytest prototypes/<question>`. The repository has no `__init__.py` files, so a prototype module must not take the name of a top-level package such as `tools`. A prototype can be deleted once the issues that build on its decision have landed.

## Manifest: kind llm

An LLM agent reasons, may call scoped tools, and returns a Pydantic model through structured outputs. `agent.yaml` holds these fields:

| Field | Meaning |
| --- | --- |
| `name`, `version`, `kind`, `description` | Shared by both kinds. `version` is a full `MAJOR.MINOR.PATCH` and `kind` is `llm` |
| `model` | Claude model id. This is configuration: verify it against `client.models.list()` in the agent's live test before committing it |
| `effort` | `output_config.effort` for every request. Use a level the API documents (`low`, `medium`, `high`, `xhigh`, `max` at the time of writing); the live test proves the model accepts it |
| `system_prompt` | Path to the system prompt, relative to the manifest's directory |
| `schemas` | Path to the module that defines the input and output models, relative to the manifest's directory |
| `input_model`, `output_model` | Names of Pydantic model classes in `schemas` |
| `tools.allow`, `tools.deny` | Dotted tool names; see [Tools](#tools) |
| `limits.max_tool_turns` | Most tool-use turns in one run. A turn is one assistant message that stops with `tool_use`, however many calls it holds |
| `limits.timeout_s` | Wall-clock limit for one run, in seconds, and the timeout of each request |
| `limits.max_output_tokens` | `max_tokens` for each request |
| `limits.max_retries` | Optional, default 2. How many times one model request that failed with a 429, a 529, a 5xx or a connection error is sent again; 0 turns retrying off. It is separate from the single validation retry. See [Request fields](#request-fields) |
| `evals.min_pass_rate` | Eval pass rate the publish gate requires, from 0 to 1 |
| `guard` | Optional. The action-guard agent that must allow each of this agent's tool calls that is not read-only, as `name@^MAJOR.MINOR`, such as `action-guard@^0.1`. Absent means no guard; see [Tools](#tools) |

Unknown keys are an error, so a typo never falls back to a default. Later tickets add fields and document them here.

Two of those fields exist now and are only declared: `sandbox` (a boolean, default `false`) says the agent may be given the sandbox-only tools, and `limits.max_tokens_total` (an integer, optional) is the most tokens one run may use. The runner enforces `max_tokens_total` after every model response: a run whose tokens pass it raises `TokenLimitError` (see [Request fields](#request-fields)). `sandbox: true` is the manifest's half of the gate described under [Tools](#tools).

The resolver validates a manifest as it loads it. `name` is lowercase words joined by hyphens and equals the agent's directory name. Tool names must match the dotted form, and a name in both `allow` and `deny` is an error; an absent `tools` block means no tools. `system_prompt` and `schemas` must be relative paths that stay inside the agent directory and name files that exist. `schemas.py` is imported by file path, and `input_model` and `output_model` must name Pydantic models in it. The resolver does not check that a tool name is known to `tools/registry.py`; the runner does when a run starts, and an unknown name raises `UnknownToolError` (exit 3).

```yaml
name: code-reviewer
version: 1.2.0
kind: llm
description: Reviews a diff for defects and reports findings with head-revision line numbers.
model: claude-sonnet-5-5        # verified by tests/live/test_code_reviewer_live.py
effort: high
system_prompt: system.md
schemas: schemas.py
input_model: ReviewInput
output_model: ReviewOutput
tools:
  allow: [git.read_file, git.show_diff, rg.search]
  deny: []
limits:
  max_tool_turns: 12
  timeout_s: 180
  max_output_tokens: 4096
evals:
  min_pass_rate: 0.75
```

An agent whose output is a set of scores, such as the judge, adds a `scoring` block: `weights` gives each of `correctness_risk`, `maintainability`, `security_risk` and `test_coverage` a weight from 0 to 1, the weights must sum to 1 (within float rounding) or the manifest fails to load, and `ship_threshold` is a number from 0 to 1. `runtime/scoring.py` is the only code that applies them: `assess(spec, output)` counts the two risks as `1 - value`, takes the weighted sum, and ships when it is at or above the threshold. Neither number goes into the system prompt or the request, so the agent cannot shade a score to reach it.

## Manifest: kind jev

A Jev agent returns probabilities over answers fixed in advance. It generates no text and calls no tools. Besides the shared fields (with `kind: jev`), the manifest holds:

| Field | Meaning |
| --- | --- |
| `model` | Jev model id, verified against `docs/jev-sdk.md` (#15) |
| `input.render` | Template rendered from the payload's fields with `str.format` names |
| `input.max_chars` | Longest rendered input. Anything longer is cut, a marker line is appended, and the run is flagged `truncated` |
| `questions` | Each has a unique `id`, a `type` of `noul`, `choice` or `score`, and `text`. A `choice` has 2 to 10 unique `options`. A `score` has a `range: [min, max]` and ordered `levels`, lowest first |
| `gates` | Each has a `name`, a `question` id, a `rule`, a `value` and an `action` of `skip` or `escalate` |
| `routing` | Entries `{when, route}`. `when` is a gate name or `question=answer`, and `route` is a list of agent names |
| `evals` | `min_pass_rate`, plus `min_recall_risky` (0.98) before any gate may skip review (#20) |

Gate rules read the question's generated output:

| Rule | Reads | Valid on | `value` |
| --- | --- | --- | --- |
| `p_above`, `p_below` | `p` for a noul; `probs[option]` for a choice or score, with an extra `option` key | all types | a probability |
| `confidence_below` | `confidence` | choice, score; noul outputs carry no confidence | a probability |
| `answer_in` | `answer` for a choice, `level` for a score | choice, score | a list of options or levels |

The generated output models are: noul `{p: float}`; choice `{answer: str, probs: dict[str, float], confidence: float}`; score `{value: float, level: str, probs: dict[str, float], confidence: float}`. `JevOutput` has one field per question id. A truncated input always forces review, whatever the gates say.

A Jev manifest pins a versioned model id such as `jev-1.14.0`, never the rolling aliases `jev-latest` or `jev-preview`, because gate thresholds are tuned against one version. `GET /v1/models` lists only the aliases, so a pinned id is verified live by checking that the response's `model` field equals it. The spike notes in [jev-sdk.md](jev-sdk.md) (#15) give the wire format behind these output models.

`registry/jev_spec.py` validates all of this when the manifest loads and reports every problem at once, naming the gate or routing entry by index. Question ids and gate names are lowercase `snake_case`, and an id must also be usable as an output field, so Python keywords and names starting with `model_` are refused. A `render` template may use only plain field names such as `{diff}`: no `{}`, `{0}`, attribute or index access, or fields nested in a format spec, and it must use at least one. A `p_above` or `p_below` gate on a choice or score needs `option`, which must be one of its options or levels; on a noul it must not set `option`. `answer_in` values must be real options or levels. A `routing` entry's `when` is a gate name, or `question=answer` for a choice or score question and one of its options or levels, and each `when` appears once. `JevSpec.output_class` builds the output model once, a `JevOutput` subclass named after the agent (`DiffTriageOutput`) with one field per question id. A choice or score field also checks that `answer` or `level` is one of the question's answers and that `probs` has exactly those keys. A score's `value` is the API's expected level mapped linearly onto `range`: with `n` levels and expected level `score` (0 to `n - 1`), `value = min + score / (n - 1) * (max - min)`. A `score` outside 0 to `n - 1` is a `JevError`, so `value` always lies within `range`.

```yaml
name: diff-triage
version: 0.1.0
kind: jev
description: Decides whether a diff needs the review panel and which reviewers see it.
model: jev-1.14.0
input:
  render: "Files changed:\n{files}\n\nDiff:\n{diff}"
  max_chars: 24000
questions:
  - id: needs_review
    type: noul
    text: Does this diff need human-quality review?
  - id: reviewers
    type: choice
    text: Which reviewers should see this diff?
    options: [code, code-security, code-perf, all]
gates:                                  # placeholder thresholds; #20 sets them
  - name: docs_only
    question: needs_review
    rule: p_below
    value: 0.05
    action: skip
  - name: unsure
    question: reviewers
    rule: confidence_below
    value: 0.6
    action: escalate
routing:
  - {when: reviewers=code, route: [code-reviewer]}
  - {when: reviewers=code-security, route: [code-reviewer, security-reviewer]}
  - {when: unsure, route: [code-reviewer, security-reviewer, perf-reviewer]}
evals:
  min_pass_rate: 0.9
```

## Jev runner

`runtime/jev_runner.py` holds `run_jev(spec, payload, trace, *, pipeline_run_id=None)`, which renders the payload through `input.render`, asks every question in one request and returns the generated output model (`spec.output_class`). A payload that lacks a field the template names raises `UsageError` before anything is sent, names the missing fields and never repeats a value, and writes no trace row.

Only `runtime/jev_transport.py` imports the TypeSafe SDK (`typesafe_sdk`, the optional `[jev]` extra). The runner loads it when a run starts, so a checkout without the extra still imports everything else. A missing extra, a missing `TYPESAFE_API_KEY` and a key the API rejects (401 or 403) all raise `AuthOrConfigError` (exit 4) at call time. The hint names the variable, never its value, and for a missing extra says `uv sync --extra jev`. Never turn on DEBUG for the `typesafe_sdk` logger: it logs request bodies, and the body is the diff under review.

| Setting | Value |
| --- | --- |
| Timeout | 5 s per request; a timeout is not retried |
| Attempts | 3, retrying only a 429 or a 5xx, after `Retry-After` when the API sends it and otherwise 0.5 s then 1 s; no wait exceeds 5 s |
| SDK retry | Off. The runner owns the retries so tests can see them |
| Retry count | The constant `MAX_ATTEMPTS = 3` in `runtime/jev_runner.py`. `JevSpec` has no `limits`, so there is no `max_retries` to read; changing the count is a code change |
| Questions | All in one request, never one request per question |

Input over `input.max_chars` is cut to that length and a final line `[truncated: input cut to N of M characters]` is appended. The run is then `truncated`, which `decision_json` records and the gates read. A `max_tokens_exceeded` 400 from the API means the input does not fit the model's context, and it is handled as one more truncation: the runner halves the text and sends it again until the request fits or the limit would fall below `MIN_CHARS` (1000), when it raises `JevError`. A run that was halved counts as truncated.

`run_jev` takes the same `budget` argument as `run`: it checks the budget before it sends, and charges the row's cost when the run ends (see [Request fields](#request-fields)).

Any other failure (a non-retryable status, retries used up, a timeout, a response that does not match the manifest) is a `JevError`, a `FactoryError` (exit 1) whose message starts with the agent's address. The pipeline turns it into an escalation with reason `jev_error` (see [Gates](#gates)). A message holds the status, the request id and, for a 400 or 422, the API's own message; it never holds the request.

## Agent address

Pipelines and manifests refer to an agent as `name@^MAJOR.MINOR`. The resolver turns the caret range into a `packaging` specifier and picks the highest version that matches:

- `^M.m` with `M` of 1 or more means `>=M.m.0,<(M+1).0.0`. `^1.2` matches 1.2.0 and 1.9.3, but not 1.1.9 or 2.0.0.
- `^0.m` means `>=0.m.0,<0.(m+1).0`, because before 1.0 a minor bump may break callers. `^0.3` matches 0.3.0 and 0.3.7, but not 0.4.0.
- Pre-release versions never match.
- When nothing matches, the resolver raises a `NotFoundError` (exit 3) that lists the versions it found.

`^MAJOR.MINOR` is the only range form; `1.2`, `~1.2`, `^1` and `^1.2.3` raise `InvalidRange` (exit 2). `resolve(name, range)` returns an `LLMSpec` or `JevSpec`. For an LLM agent the attributes #5 reads are `model`, `effort`, `tools.allow`, `tools.deny`, `limits.*`, `system_prompt_file`, and the loaded classes `input_class` and `output_class`; `input_model` and `output_model` stay the class names from the manifest. A missing agent raises `AgentNotFound`; a range with no match raises `VersionNotFound`, whose `available` attribute and hint list the versions found.

A resolved agent is written `name@MAJOR.MINOR.PATCH`, for example `code-reviewer@1.2.0`. That is the form in trace rows and tags.

`resolve` and `list_agents` take `published_only: bool = False`. When true, a version without a row in the `published` table of the trace store is ignored, so an unpublished 1.3.0 never shadows a published 1.2.0. The table is read from the same path `Trace` uses (`FACTORY_TRACE_DB`, else `data/traces.duckdb`), and a store that does not exist yet means nothing is published. When versions match the range but none is published, `resolve` raises `NotPublished` (a `VersionNotFound`, exit 3) whose hint says to run `factory publish-check` or pass `--allow-unpublished`. Pipelines (#14) resolve with `published_only=True` unless the caller passes `--allow-unpublished`; every other command resolves with the default, so `factory eval` and `factory publish-check` can still reach a version that has not been published.

Any change to a prompt, schema or question bumps the version in the same commit; `scripts/check_version_bump.py` enforces it (see "CLI shape"). A change that breaks the input or output model bumps the major. A change that alters behaviour (prompt, questions, model, effort, tools, limits, gates, a compatible schema addition) bumps the minor. A change that never reaches the model (description, eval cases) bumps the patch.

## Output contract

An LLM agent returns exactly its output model, through structured outputs (`output_config.format`). The runtime validates the result with Pydantic:

- The answer is the text of the final assistant message, the one that stops with `end_turn` or `stop_sequence`.
- On a validation error, it sends the field paths and messages back to the model once, without the values, and asks for the corrected JSON object.
- A second failure raises `OutputValidationError`, an `AgentError` carrying the transcript so far. Nothing falls back silently.
- The stop reasons `refusal` and `max_tokens` raise their own typed errors and are not retried as validation failures. A refusal raises `RefusalError`, which carries `stop_details.category`. `max_tokens` and `model_context_window_exceeded` raise `OutputTruncatedError`, which carries the stop reason.
- `pause_turn` continues the loop. Any other stop reason raises `ModelAPIError`, and so does a loop that ends without a final answer. That includes `compaction`, since the runner does not enable compaction.
- There is no `submit_result` tool and no forced `tool_choice`. Current models answer `tool_choice` of type `any` or `tool` with a 400, and they also reject an assistant prefill.

Let the SDK build `format` from the Pydantic model rather than hand-writing the schema:

```json
"output_config": {
  "effort": "high",
  "format": {"type": "json_schema", "schema": {"...": "ReviewOutput as JSON schema"}}
}
```

## Request fields

Never send `temperature`, `top_p` or `top_k`; current models reject them with a 400. Never send `thinking.budget_tokens` either.

Every request sends:

- `thinking: {"type": "adaptive"}`;
- `output_config.effort` from the manifest. Always set it explicitly, because the default differs between models;
- `max_tokens` from `limits.max_output_tokens`;
- the system prompt as one text block with `cache_control: {"type": "ephemeral"}`. A prompt shorter than the model's minimum cacheable prefix is sent but not cached.

`build_request` in `runtime/runner.py` builds every request in one place, and its tests assert that the keys in `FORBIDDEN_REQUEST_KEYS` never appear. Besides the sampling keys, that set holds `tool_choice` and the SDK runner's `max_iterations`, because the runner counts tool turns itself against `limits.max_tool_turns`. Thinking blocks go back to the model unchanged, signatures included.

Two arguments go to the SDK's tool runner rather than onto the wire:

- `thread=False`, so every turn sends the full history. The runner holds no server-side thread, and its transcript alone can replay a conversation.
- `timeout=limits.timeout_s`, for each request. The runner also checks a deadline of `limits.timeout_s` after each turn, and either one raises `AgentTimeoutError`. The runner builds its client with the SDK's own retries off (`max_retries=0`), so a request that timed out is not repeated and `timeout_s` holds.

```json
{
  "model": "claude-sonnet-5-5",
  "max_tokens": 4096,
  "thinking": {"type": "adaptive"},
  "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": {}}},
  "system": [{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}],
  "messages": []
}
```

Retries and limits belong to the runner, not the SDK. A request that fails with a 429, a 529, any other 5xx or a connection error is sent again up to `limits.max_retries` times (default 2), waiting 0.5 s, then 1 s, doubling to at most 8 s, or the `Retry-After` the API sent, also capped at 8 s. A reply from the model resets the count, so the limit applies to a run of consecutive failures. When the retries are used up the run raises `ModelAPIError` ("after N retries") with the transcript, and so it does when the next wait would pass the run's `timeout_s`. Other statuses, such as a 400 or a 401, and timeouts are not retried. This is the API-level retry; the one retry on an output that fails validation is separate and unchanged.

`limits.max_tokens_total` counts input, output, cache-read and cache-write tokens, summed over every response of the run. The task text says "input and output"; the cached tokens are counted as well because the API's `input_tokens` leaves them out, and a long cached prompt re-read on every turn is most of what a tool-using run spends. When the sum passes the limit the run raises `TokenLimitError` (an `AgentError`, exit 1) with `limit`, `used` and the transcript, and the trace row records it. The cached tokens stay in the count although a cache read costs a small fraction of an input token (decision in #26): the limit bounds how many tokens and how much context a run works through, not what it costs. Dollars are the `Budget`'s job, and it prices cache reads and cache writes at their own rates through `runtime/pricing.py`.

A pipeline shares one `Budget` (`runtime/budget.py`) between every run it starts, passed as the `budget` argument of `run`, `arun` and `run_jev`, handed on to the guard agent's runs, and given to the eval graders. A run checks it before starting and refuses to start once nothing is left. It then charges each model message's cost as that message arrives, so runs sharing the budget, on threads or gathered on one event loop, see each other's spend while they run; a Jev call or an `LLMJudge` call is one charge, made when it returns. The charge that takes the spend past the cap raises `BudgetExceeded` (exit 1, the spend so far in the message), and a pipeline lets it end the whole pipeline instead of counting it as one failed agent. A run that fails for another reason only adds its uncharged cost, so the budget never hides the error that ended it.

A request cannot be stopped halfway, so the total can pass the cap. The bound is the cap plus one model message, one Jev call or one judge call per run in flight when the cap was passed; runs not yet started are refused. Before #26 a run compared only its own cost with what was left, so three concurrent runs at $0.20 a message under a $0.50 cap could each spend $0.60 and reach $1.80 together; under the per-message charge they stop at $1.10 at most, and `tests/runtime/test_async_runner.py` holds that bound under `asyncio.gather`. The default cap is `DEFAULT_BUDGET_USD` in `runtime/budget.py`, and `factory review --budget-usd` overrides it. `factory eval` and `factory publish-check` use the default and share one budget over all of their cases, and the `LLMJudge` and `JevJudge` graders are charged to it too.

## Async runtime

`runtime/async_runner.py` holds `arun`, the agent run on the SDK's async Tool Runner. It keeps the whole contract of `run`: payload validation, scoped tools and their hooks, the one validation retry, the typed errors, the limits, the budget and exactly one trace row. There is one conversation loop, in `arun`; `run` is its blocking face and drives it on an event loop of its own, so the sync CLI entry points are unchanged. Calling `run` on a thread that is already running a loop raises `UsageError`; await `arun` there.

- `arun` builds an `anthropic.AsyncAnthropic(max_retries=0)` when no client is passed, and `run` builds an `anthropic.Anthropic(max_retries=0)`; either accepts the other kind, and a synchronous client is stepped on worker threads.
- Tool calls and hooks are blocking code, so each runs on a worker thread through `asyncio.to_thread`, one call at a time per run.
- An `AsyncAnthropic` client belongs to the loop it is used on. When a hook (the action guard) calls `run` with the outer run's async client, `run` hands that run to the outer loop and blocks its worker thread until it ends.
- A pipeline that runs agents side by side gathers `arun` calls with `asyncio.gather` on one loop and shares one `Trace` and one `Budget` between them, as the review panel does with its reviewers. `factory eval` still runs its cases through `run` on a thread pool; each worker thread gets a loop of its own, and the shared `Trace` and `Budget` lock their own state.

## Tools

Manifests name tools with dots: `<group>.<action>`, lowercase, with no double underscore inside a part (`git.read_file`). The API does not accept dots in tool names, so on the wire each dot becomes `__` (`git__read_file`). The rewrite is reversible, and `tools/registry.py` maps a wire name back to the dotted name.

Allow and deny rules:

- `tools.allow` is the complete set of tools an agent gets.
- The runner gives the SDK only the allowed tools, so a call to a denied, unlisted or made-up tool gets the SDK's not-found error tool result and never runs.
- A name that appears in both lists is an error at load time. An unknown name in either list is an error when a run starts.
- Tools are read-only. A tool that writes may be allowed only for an agent that runs inside the sandbox (#24), behind the action guard (#23).
- A tool reads only inside the repository root it is given. A path that escapes the root, whether through `..`, an absolute path or a symlink, is rejected with a typed error. Subprocesses run from an argument list, never through a shell, and output is capped with a truncation marker.

A tool implementation is a function `impl(repo_path, **arguments, max_bytes=MAX_OUTPUT_BYTES) -> str`. The definition in `TOOLS` lists only the arguments the model supplies; the runner passes `repo_path` itself and never forwards a model-supplied `max_bytes`. `repo_path` must be the top of a git work tree. A path is resolved against the real disk and must stay inside it, but an absolute or `..` path that ends up inside is accepted. Output beyond `MAX_OUTPUT_BYTES` (64 KiB) is cut on a character boundary and ends with one line, `[truncated: N bytes dropped, limit M bytes]`. Every failure is a `ToolError` subclass from `tools/_common.py`, so the runner can turn one clause into an error tool result, and its exit code follows the usual bases (`PathEscapeError` and `ToolInputError` 2, `ToolNotFoundError` and `UnknownToolError` 3, a missing `git` or `rg` 4). `tools.dotted_name(wire)` raises `UnknownToolError` for a name the model made up.

The runner calls a hook around every tool execution. The protocol is `ToolHook` in `runtime/hooks.py`, and `run(..., hook=...)` takes a new one per run; the default, `AllowAll`, allows everything.

- For each call the runner builds a `ToolCall` with the dotted `name`, the model's `arguments` and the calling `agent` address, and asks `before_tool(call)`. It returns `ToolDecision.allow()` or `ToolDecision.deny(message)`.
- A denied call never runs. Its message goes back to the model as an error tool result, and `after_tool` is not called. A denial never ends a run by itself.
- An allowed call has its arguments checked against the tool's schema and then runs. The hook sees `after_tool(call, ToolResult(content, is_error))` with what went back to the model.
- A `ToolError` from a tool goes back to the model as an error result, `message (hint: ...)`. `ToolUnavailableError` (a missing `git` or `rg`) ends the run instead, since the model cannot work around it.
- A hook that must end the run, such as the action guard (#23), raises a `FactoryError` from `before_tool`. `run` re-raises it unchanged once the turn is over. Any other exception from a hook or a tool ends the run with `ToolCrashError`, the original exception as its `__cause__`.
- Once a run is ending, any further calls in the same turn get the error result `not run: the run is ending`.

```python
class CountDenials:
    def __init__(self) -> None:
        self.denied = 0

    def before_tool(self, call: ToolCall) -> ToolDecision:
        if call.name == "rg.search":
            self.denied += 1
            return ToolDecision.deny("rg.search is off for this run")
        return ToolDecision.allow()

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        return None
```

```yaml
tools:
  allow: [git.read_file, git.show_diff]   # sent as git__read_file, git__show_diff
  deny: [rg.search]
```

`tools.registry.READ_ONLY` is the set of tools that cannot change anything: `git.read_file`, `git.show_diff` and `rg.search`. A tool that is not in it, including a name the registry does not know, is treated as one that changes something. Add a tool to `READ_ONLY` only if it reads and nothing else.

An agent whose manifest sets `guard` gets an action guard. `runtime/guard.py` holds `GuardHook`, a `ToolHook` that `run` installs when `spec.guard` is set, in front of any `hook` the caller passed: the caller's hook decides first, and only a call it allows goes to the guard. For a call to a tool in `READ_ONLY` the hook allows and writes nothing. For any other call it renders the tool name, the arguments, the worktree root (`repo_path`, resolved) and a task summary (`name@version: description` of the guarded agent) as the guard agent's input, runs the guard through `runtime.runner.run`, and acts on its `allow` and `reason`. A denial goes back to the model as the error tool result `<tool> refused by the action guard: <reason>`. The third denial in one run raises `GuardError` instead (a `FactoryError`, exit 1), which `run` re-raises unchanged and records in the guarded run's own trace row. Allowed calls do not reset the count. A guard run that fails with an `AgentError` also raises `GuardError`: a call is never allowed on a guess. A missing API key or any other `FactoryError` from the guard run propagates as it is.

Every guard decision is one trace row, whether it allows or refuses. Its `kind` and `agent_ref` are the guard agent's, and `decision_json` is `{"parent_run_id", "allow", "reason"}`, `parent_run_id` being the `run_id` of the run whose call was judged. For an LLM guard the row is the guard run's own row, so it carries the tokens and the cost. The guard agent has no tools, so it never recurses.

The guard comes in two forms, chosen by the `kind` of the agent that `guard` names. `kind: llm` is the form that ships, `action-guard@0.1` on `claude-haiku-5-5` at effort `low`. `kind: jev` would be a noul answering "is this tool call safe inside the sandbox?" and applies once Jev is a go; it is not wired, and `build_guard_hook` raises `UsageError` for it, since turning a probability into allow or deny needs a threshold, and thresholds live in `runtime/gates.py`.

The sandbox-only tools are `fs.write_file` and `shell.run` (`SANDBOX_ONLY` in `tools/registry.py`); `tool_defs(allow, deny, sandbox=...)` raises `ToolListError` for either when the manifest does not say `sandbox: true`, and the runner passes the flag. `fs.write_file(path, content)` replaces or creates one file inside the repository root, refuses anything under `.git` and content over 256 KiB. `shell.run(argv)` takes an argument list and starts only `pytest`, `python`, `uv` or a read-only `git` subcommand, as a bare name, inside a throwaway container with no network that sees only the work tree (option (a) of `docs/decisions/0001-fixer-runtime.md`). A non-zero exit is a normal result, reported as `exit code N` and the output; a command past 120 s is killed and reports 124. The image is built once, with the network, by `docker build -t factory-sandbox:0.1 -f tools/sandbox.Dockerfile tools`; a missing Docker or image raises `ToolUnavailableError` and ends the run. `fs.list_files` and `fs.read_file` are read-only and read the work tree as it is on disk, edits included, so an agent that writes can read back what it wrote. Both are in `READ_ONLY`, so the action guard lets them through; every tool is in exactly one of `READ_ONLY` and `SANDBOX_ONLY`.

The fix loop (`pipelines/fix_loop.py`, #24) adds a rule the tools do not enforce: only the test-writer changes tests. It lives in the pipeline, not in `fs.write_file`, because only the pipeline knows which agent is writing and what the test-writer changed. `TestFileGuard` is the fixer's `hook`, so it decides before the action guard: it refuses an `fs.write_file` to a path that `evals.graders.sandbox_tests.is_test_file` matches, that the test-writer changed (as its commit shows, not what it reports), that configures pytest at the root of the clone (`pyproject.toml`, `setup.cfg`, `pytest.ini` and the like), or that would shadow a module the runner imports (best effort; the panel is the real check), checking the path as written and again with symlinks resolved. After the fixer's run, a check on the host refuses the same paths when they changed by another route, such as a script started through `shell.run`. A refusal ends the run rather than going back to the model as a denial, because a fixer that wants to change a test either thinks the test is wrong or is working around it, and both need a person. It writes one trace row, `tag` `fix:test_write_refused` (`TEST_WRITE_REFUSED_TAG`), `kind` `llm`, `agent_ref` the fixer, `cost_usd` 0, `error` the message and `decision_json` `{"allow": false, "path", "reason", "check"}`, `check` being `hook` or `post-check`, and raises `TestFileWriteError`, a `FactoryError` (exit 1). The agents work in a clone (`git clone --no-hardlinks`, `pipelines/sandbox.py`), not a `git worktree`: a worktree's commits and admin entries land in the source repository's `.git`, which the fix loop never writes, and its `.git` file points outside the work tree, where the sandbox, which mounts only the work tree and its `.git` read-only, cannot follow. Before the suite the clone is reset to the fixer's commit with its untracked and ignored files deleted (`pipelines.sandbox.clean`), so the suite runs over the commits alone. The suite gets 600 s (`SUITE_TIMEOUT_S`) instead of a tool call's 120 s, and `FACTORY_SANDBOX_IMAGE` names another image than `factory-sandbox:0.1`, such as one holding a target repository's test dependencies.

## Fixtures

A fixture is a directory `fixtures/src/<name>/` holding a `base/` tree, a `head/` tree and a `defects.yaml`; hand-built ones live in `fixtures/handmade/<name>/` in the same shape. `uv run python -m fixtures.build [name ...]` turns each into a git repository under `fixtures/repos/<name>/` (gitignored) with branches `base` and `head`, one commit each. Identity, dates and branch names are fixed and no hooks or git config apply, so the SHAs are reproducible; a rebuild deletes the old repository first. The directory name is the fixture's name and the handle an eval case uses, so a name is never renamed or reused. A new fixture only needs its directory: `discover` picks it up, and `ROOTS` in `fixtures/build.py` lists the source roots.

`defects.yaml` lists the planted defects: `id`, `file`, `line_start`, `line_end` (1-based, inclusive, in the head tree), `severity` (`info`, `minor`, `major` or `blocker`, as in a finding), `category` (a lowercase slug; so far `logic`, `style`, `security`, `performance`, `testing`, `hardcoded-secret`, `path-traversal`; a fixture planted for an agent that fixes its category strings in its prompt, such as security-reviewer, uses one of those strings), `title`, `description` and a `snippet` that must appear inside the window. The list is empty for a fixture a reviewer should pass without findings. The build fails when a window has drifted off its code. Source trees are linted and type-checked with the rest of the repository, so a planted defect must be one ruff and ty accept.

A reviewer that works on one kind of defect names the categories it may use in its own `system.md`; `Finding.category` stays a free string and gains no enum. The performance reviewer uses `algorithmic-complexity`, `n-plus-one`, `missing-index`, `unbounded-memory`, `blocking-call` and, when none of those fits, `performance`, so a fixture planted for it takes its `category` from that list (`performance` is also what the older `n-plus-one` fixture records). Because the reviewers run in the same eval harness, a perf fixture's defect is graded on file, line window and severity, not on the category string.

Two variants serve the agents that write tests and fix code. `off-by-one-red` is `off-by-one` plus a test that fails on `head` and passes on `base`, the input of the fixer; it is the one fixture whose own suite is red on `head` (`tests/fixtures/test_fixtures_build.py` expects that). `untested-change-covered` is `untested-change` with a passing test file, so the fixer has nothing to fix and `defects.yaml` has `defects: []`. The fixtures have no `pyproject.toml`; their tests run with `python -m pytest -q` from the repository root.

```yaml
fixture: off-by-one
summary: Running-sum rewrite that drops the last window.
defects:
  - {id: last-window-dropped, file: series/rolling.py, line_start: 12, line_end: 13,
     severity: major, category: logic, title: ..., description: ..., snippet: "len(values) - 1"}
```

## Eval cases

`agents/<name>/evals/cases.yaml` holds a top-level `cases` list. A case has `id` (unique in the file), `fixture` (a fixture directory name), `payload` (the agent input without `repo_path`, which the eval runner sets to the built fixture repository), `expected` and `grader`. `expected` has `verdict` and `defects`; each expected defect copies `id`, `file`, `line_start` and `line_end` from the fixture's `defects.yaml` and names `severity_floor`, the lowest severity a matching finding may have. A finding matches a defect when it names the same file and its line window overlaps the defect's. `grader` is `llm_judge` for LLM agents.

```yaml
cases:
  - id: off-by-one
    fixture: off-by-one
    payload: {base: base, head: head}
    expected:
      verdict: request_changes
      defects:
        - {id: last-window-dropped, file: series/rolling.py, line_start: 12, line_end: 13, severity_floor: major}
    grader: llm_judge
```

`evals.run.evaluate` runs each case through `runtime.runner.run` with the tag `eval:<name>@<version>`, so `tag` on the trace rows selects an eval's runs. Grading is deterministic first: the verdict must equal the expected one, and each expected defect needs a finding with the same file, an overlapping line window and a severity at or above `severity_floor`. A case that fails those checks fails without a judge call. A case that passes them goes to the `llm_judge` grader (`evals/graders/llm_judge.py`), one structured-output call to `claude-haiku-5-5` that returns `pass`, `score` and `reason` and is traced under the same tag. A typed agent failure fails its own case with the error text; a missing key or a payload the agent rejects ends the whole eval. The eval passes when `passed_count / total` reaches the agent's `evals.min_pass_rate`.

An agent whose output has no verdict, such as the judge, sets `verdict: null`, which skips the verdict check; its `merged_findings` are matched against `defects` the way `findings` are. `expected` may also carry `criteria`, prose the `llm_judge` grader must see met in full, such as "the rationale names the failed reviewer". The deterministic checks do not read it. The grader receives `expected` without its null fields.

An agent whose output is neither verdicts nor findings, such as the action guard, which returns `allow` and `reason`, uses `verdict: null`, `defects: []` and `criteria` that says whether the call must be allowed or refused and what the reason has to name. Its cases need a fixture name only because the format asks for one; the payload does not mention it and has no `repo_path` field for the eval runner to fill in. A deterministic grader that reads `allow` would need a change in `evals/`.

A case with `grader: jev` is graded by a Jev noul question instead of a Claude call (`evals/graders/jev.py`, manifest `evals/graders/jev_grader.yaml`, not an agent in the registry). After the deterministic checks, the grader asks "does this answer meet the expectation?" about the case's `expected` and the agent's answer, passes it when `p` is above the manifest's `matches` gate, and traces the call under the eval's tag. If the Jev call fails with a `JevError` it falls back to the `llm_judge` grader and marks the case `grader_fallback`; a missing key and an exhausted budget still end the eval. A Jev agent's answer reaches the graders as `{question_id: picked}`, so `expected.verdict` is compared with the option its `verdict` question picked. `python -m evals.graders.jev [name@^M.m ...] [--json]` runs both graders over real answers and prints how often they agree; it calls live models, so name the agents it should run. The threshold and the measured agreement are in `docs/jev-sdk.md`.

The `sandbox_tests` grader (`evals/graders/sandbox_tests.py`) is for agents that write tests or code, which are graded on the work tree they leave and not on their answer alone. It needs `expected.verdict: null` and an `expected.run` block, and it never calls a model by itself. An agent with `sandbox: true` runs on a private copy of the case's fixture, so cases do not see each other's edits. `expected.run` may hold `new_tests` (`fail` or `pass`: pytest over the files in the answer's `test_files` exits 1, or 0), `passes_on_base` (also `pass` once the non-test files are put back to the fixture's `base` state, which shows the test fails for the defect and nothing else), `suite` (`pass`: the whole suite exits 0), and `production` and `tests` (`changed` or `unchanged`: whether any non-test or test file differs from the fixture's HEAD). A failed check fails the case with a reason and skips the judge; a case whose checks pass goes on to the `llm_judge` grader with `criteria`. The grader needs Docker and the sandbox image.

## Gates

Thresholds, skip decisions and routing are code in `runtime/gates.py`. The thresholds come from the manifest, never from a prompt, and no model decides them. A gate evaluation returns a `Decision`:

```python
@dataclass(frozen=True, slots=True)
class Decision:
    skip: bool  # skip the full review
    escalate: bool  # hand the decision to Claude or a human
    route: tuple[str, ...]  # agent names to run, in order, without duplicates
    reason: str  # gate name, or "truncated", "jev_error" or "no_gate"


Decision(skip=False, escalate=True, route=("code-reviewer", "security-reviewer"), reason="unsure")
```

`route` is a tuple so that a frozen `Decision` really cannot change. These rules hold whatever the gates say:

- An escalation clears `skip`.
- A truncated input yields `skip=False, escalate=True, route=()` with reason `truncated`, whatever the answers: review everything.
- A Jev failure escalates with reason `jev_error`.
- Jev never has the last word on anything consequential.

`decide(spec, output, truncated)` in `runtime/gates.py` evaluates `JevSpec.gates` and `JevSpec.routing` on a Jev output. A gate fires on a strict comparison, and `skip` is set when a skip gate fired and no escalate gate did. `route` is the union, in manifest order, of the routing entries whose `when` is a fired gate or `question=answer` for the answer given. `reason` is the first escalating gate, else the first skipping one, else the `when` of the first matching route, else `no_gate`. `ROUTE_ONLY = True` in the same module means a pipeline logs the decision and still runs everything (#20 turns it off): the review panel runs `diff-triage@^0.2` after `show_diff`, writes the decision and the probabilities to one extra trace row (`kind` `jev`, `tag` `triage:decision`, no cost of its own) and then runs every selected reviewer. Any exception from the Jev call becomes `Decision(skip=False, escalate=True, route=(), reason="jev_error")` and the panel goes on. The panel raises `NotImplementedError` if `ROUTE_ONLY` is false, because nothing acts on a decision yet.

The triage decision row's `cost_usd` is 0.0 because the `run_jev` row under the same `pipeline_run_id` carries the real cost of the call; charging both would count it twice in `factory cost` and `Trace.cost_by_pipeline_run`. `scripts/calibrate.py` and `scripts/shadow_report.py` rely on this and read a run's cost as the sum over its pipeline run id. Calibration runs use pipeline run ids starting with `calibrate-`, so the shadow report can leave them out ([jev-calibration.md](jev-calibration.md)).

The judge gate (`decide_judge_gate` in `runtime/gates.py`, agent `judge-gate@^0.1`) is the one place a Jev decision does skip work. After the reviewers have run, the panel renders their reviews as text and asks two questions: whether the verdict is clear-cut (noul `clear_cut`) and which of ship, request changes or unclear it is (choice `verdict`). The skip gate fires only when `clear_cut` is above 0.85 and neither escalating gate did: the `verdict` confidence below 0.8, or the answer `unclear`. A failed reviewer, a truncated rendering and a failed Jev call all send the change to the judge, with reasons `reviewer_failed`, `truncated` and `jev_error`. When the gate skips the judge, the verdict is the gate's `verdict` answer, computed in code, and `ship` is true only for `ship`. The gate decision goes to one extra trace row tagged `judge_gate:decision`, with no cost of its own, whose `decision_json` also holds the path (`skipped_judge` or `escalated`) and an estimate of what the judge would have cost. The gate call itself is charged to the panel's budget.

## Trace row

Every agent run, successful or not, writes exactly one row to the DuckDB trace store. The default path is `data/traces.duckdb`, overridden by an argument or by `FACTORY_TRACE_DB` (#6).

The LLM runner writes the row in a `finally` block, so a failed run still records its usage and `error` as `<ErrorClass>: <message>`. Usage is summed over every response, the validation retry included. Two checks come before the run starts and write no row, because there is nothing valid to record: a payload that fails the input model, and a missing `repo_path` for an agent that allows tools. Both raise `UsageError`. The Jev runner also writes its row in a `finally` block, with `kind` `jev`, `output_tokens` 0, `input_tokens` from the response's usage and `cost_usd` from `jev-1.14.0` in `runtime/pricing.py` (input tokens only, no cache). Its `input_json` is the payload and its `output_json` holds the full probability vectors. A payload that does not match the template raises `UsageError` and writes no row.

| Column | Type | Meaning |
| --- | --- | --- |
| `run_id` | VARCHAR | UUID of this run |
| `pipeline_run_id` | VARCHAR, null | UUID shared by every run in one pipeline invocation |
| `agent_ref` | VARCHAR | Resolved address, e.g. `code-reviewer@1.2.0` |
| `kind` | VARCHAR | `llm` or `jev` |
| `started_at` | TIMESTAMPTZ | Start time, in UTC |
| `duration_ms` | BIGINT | Wall-clock duration |
| `input_tokens`, `output_tokens` | BIGINT, null | Summed over all turns. As in the API's usage, `input_tokens` excludes cache reads and writes |
| `cache_read_tokens`, `cache_write_tokens` | BIGINT, null | Summed over all turns |
| `cost_usd` | DOUBLE, null | From `runtime/pricing.py`; null when the model has no price |
| `decision_json` | JSON, null | The `Decision` and gate inputs for a Jev or guard run. A Jev run records `{"truncated", "model"}`, `model` being the one that answered, or null when none did |
| `input_json` | JSON | The validated input payload |
| `output_json` | JSON, null | The validated output, or the full probability vectors for Jev; null on failure |
| `error` | VARCHAR, null | Error class and message when the run failed |
| `tag` | VARCHAR, null | Set by the eval runner and publish gate, e.g. `eval:code-reviewer@1.2.0`. `factory cost` reads the prefix: `eval:` and `publish:` runs are totalled apart from the rest. `factory publish-check` currently tags its runs `eval:`, so the `publish` total stays at zero until something tags `publish:` |

A guard decision is a row of its own, written by `runtime/guard.py`. Its `decision_json` is `{"parent_run_id", "allow", "reason"}`, `parent_run_id` is the `run_id` of the guarded run, and `kind` and `agent_ref` name the guard agent. Select a run's decisions with `json_extract_string(decision_json, '$.parent_run_id') = <run_id>`.

A version becomes publishable by passing its evals. Migration 2 adds the `published` table, written only by `factory publish-check` through `Trace.record_published(agent, version, pass_rate, git_sha)` and read back with `Trace.published()`. It is an append-only history with no key: publishing a version again adds a row, and a version counts as published once it has any.

| Column | Type | Meaning |
| --- | --- | --- |
| `agent`, `version` | VARCHAR | The agent name and its `MAJOR.MINOR.PATCH`, as in the manifest |
| `pass_rate` | DOUBLE | The eval pass rate that earned the row |
| `published_at` | TIMESTAMPTZ | When the row was written, in UTC |
| `git_sha` | VARCHAR | `HEAD` of the repository when the check ran |

A `Trace` holds one DuckDB connection for its lifetime and opens it on first use, rather than opening per call. DuckDB allows one read-write process per file, so holding the connection keeps the single writer (#26) in charge of the file and saves a file open on every run; close it with `close()` or a `with` block. That one connection is shared by runs gathered on an event loop and by hooks and guard runs on worker threads, and a DuckDB connection must not be used from two threads at once, so every public `Trace` method holds the instance's reentrant lock (#26). It is a lock rather than a writer task and a queue: a row is one fast insert, and a queue would need a flush before every read and a loop that callers on worker threads do not have. Schema changes are numbered migrations in `runtime/trace.py`: append a `(version, sql)` pair, never edit a shipped one, and `ensure_schema()` applies the missing ones and records them in `schema_version`. `cost_usd` is null for a model with no entry in `runtime/pricing.py`, and aggregates treat null as zero.

## Secrets

`ANTHROPIC_API_KEY` and the Jev key are read from the environment and from nowhere else. The Jev key is `TYPESAFE_API_KEY`, confirmed by #15 in [jev-sdk.md](jev-sdk.md).

- Store keys with `agb sys secrets set <NAME>`, which writes them to a mode-600 `secrets.env`.
- The factory never reads that file. Whoever starts the factory exports the key: the shell, CI, or the `agb factory` wrapper (#30). In a container the same variables come from an env file, `.env` copied from `.env.example`, passed with `--env-file` or Compose's `env_file` ([Deployment](#deployment)).
- A missing key raises `AuthOrConfigError` (exit 4) with a hint that names the variable, never its value. So does a key the API rejects with a 401 or 403, with the same hint.
- No secret goes into the repository, a test, a config file, a container image, a trace row, a log line or an error message. The `typesafe_sdk` logger logs each request id at INFO, so a deployed factory runs it at WARNING (`TYPESAFE_LOG_LEVEL=warning`, which the SDK reads at import) and never at DEBUG.
- Tests that need a live key carry a marker, and `tests/conftest.py` skips each marker when its variable is unset: `live` needs `ANTHROPIC_API_KEY` and `live_jev` needs `TYPESAFE_API_KEY`. The two are independent, so having one key does not run the other's tests.

```python
if not os.environ.get("ANTHROPIC_API_KEY"):
    raise AuthOrConfigError(
        "ANTHROPIC_API_KEY is not set",
        hint="store it with `agb sys secrets set ANTHROPIC_API_KEY` and export it",
    )
```

## Deployment

The factory ships as one image (`Dockerfile`, multi-stage, uv from `uv.lock`, non-root user, `ENTRYPOINT ["factory"]`, `/app/data` as the volume that holds `FACTORY_TRACE_DB`) and runs from `compose.yaml` with `env_file: .env`. A key is passed at run time and is never a `COPY`, an `ARG`, an `ENV` or a file in the image. `scripts/scan_image.py` greps every layer and the image configuration for `sk-ant-` and `whk_` strings and CI fails the pull request on a match, so a change that touches the `Dockerfile` or `.dockerignore` keeps `uv run pytest tests/deploy` and that scan green. The default container publishes no port and has no Docker socket; the optional HTTP trigger in `api/` runs only under the `api` profile, on `127.0.0.1`, without authentication. Build, rotate, upgrade and the CasaOS import are in [deploy.md](deploy.md).

## Changelog and releases

Every change adds a line under `## [Unreleased]` in `CHANGELOG.md`, in the same pull request, in Keep a Changelog 1.1.0 form and written for a user, not as a commit title. The package version lives only in `version` in `pyproject.toml`; code reads it with `importlib.metadata.version('agb-agent-factory')` and never carries a `__version__` literal. The release steps, and what counts as a patch, minor or major release, are in [releasing.md](releasing.md).

## CLI shape

Every command has the form `factory <verb> [args] [--json]`.

- Without `--json`, the result goes to stdout as plain text, and progress and logs go to stderr.
- With `--json`, stdout carries exactly one JSON document and nothing else. Prose, progress and logs go to stderr.
- The document is a JSON object, so a command can add a key later without breaking consumers. Keys are snake_case. Money is a float under a key ending in `_usd`, durations are integers under a key ending in `_ms`, and timestamps are ISO 8601 in UTC.
- A negative result still prints its document and exits 1. Examples are `ship: false` from `factory review`, or a pass rate below the floor.
- On an error under `--json`, stdout stays empty and stderr ends with the envelope from `error_envelope()` on one line. Without `--json`, stderr gets the message and, if there is one, the hint.
- Usage errors that argparse catches itself print its usage message instead, and still exit 2.

`factory list` prints one `name@version<TAB>kind` line per version found, lowest version first within each agent, and under `--json` the document `{"agents": [{"name", "version", "kind"}]}`.

```console
$ factory list --json
{"agents": [{"name": "code-reviewer", "version": "1.2.0", "kind": "llm"}]}
$ factory run no-such-agent@^1.0 --json 2>&1 >/dev/null
{"error": {"code": 3, "message": "no agent named 'no-such-agent'", "hint": "run `factory list`"}}
```

`factory eval <name>[@range] [--json] [--workers N]` runs the agent's `evals/cases.yaml` on `--workers` threads (default 4) and prints a per-case table on stderr. Under `--json`, stdout carries one object with `agent`, `version`, `ref`, `tag`, `eval_run_id`, `min_pass_rate`, `pass_rate`, `passed`, `total`, `passed_count`, `cost_usd`, `duration_ms` and `cases`; each case has `id`, `fixture`, `grader`, `passed`, `score`, `reason`, `deterministic`, `judge`, `grader_fallback` (true when the `jev` grader failed and the LLM judge decided), `error` and `duration_ms`. It exits 0 at or above the minimum pass rate and 1 below it, with the document printed either way. `tests/evals/golden/code-reviewer-eval.json` is a normalised example.

`factory publish-check <name>[@range] [--json] [--workers N]` runs the same evaluation as `factory eval` and, only when it passes, writes exactly one row to the `published` table (see "Trace row"). It writes nothing on a fail. Output and exit codes match `factory eval`: the table on stderr, and under `--json` one object holding the eval keys plus `published` (a boolean) and `git_sha`, printed also on a fail. It exits 0 when the version was published and 1 when the pass rate is below the floor, 3 for an unknown agent or version, and 4 when git cannot give the commit to record. Commit the change first: the recorded `git_sha` is `HEAD`, so it names the commit that carries the prompt only if the working tree is clean.

`factory review <repo_path> --base <ref> --head <ref> [--json] [--reviewers a,b] [--allow-unpublished] [--no-triage] [--no-judge-gate] [--budget-usd N]` runs the review panel in `pipelines/review_panel.py`. It resolves `code-reviewer@^1.2`, `security-reviewer@^1.0`, `perf-reviewer@^0.3`, `judge@^2.0` and, unless `--no-triage`, `diff-triage@^0.2` up front, published versions only unless `--allow-unpublished`, and exits 3 before any request if one is missing. `--reviewers` runs a subset of the three, in panel order; an unknown name is a usage error. The reviewers run concurrently, gathered with `asyncio.gather` on `arun` on one event loop that the command starts, and every run, the judge's included, carries the same `pipeline_run_id`, so `Trace.cost_by_pipeline_run` gives the cost of the whole panel. A reviewer that raises is reported with its error and the panel goes on; the judge sees it under `failed_reviewers`. If all of them fail, nothing is judged and the command exits 1. The judge returns the four scores and `runtime.scoring.assess` turns them into `weighted_score` and `ship`. Under `--json`, stdout carries one object with `pipeline_run_id`, `base`, `head`, `reviewers` (each with `agent`, `version`, `ref`, `status`, `verdict`, `finding_count` and `error`), `judge` (`ref` and `output`), `weighted_score`, `ship`, `total_cost_usd`, `duration_ms`, `triage` and `judge_gate`, and the text report goes to stderr. It exits 0 when `ship` is true, 1 when it is false or on a hard error, 2 for usage, 3 for an unknown agent, repository or ref, and 4 when `ANTHROPIC_API_KEY` is not set. `triage` holds `status` (`ok`, `jev_error` or `disabled` for `--no-triage`), `ref`, `decision` (the `Decision` fields), `probabilities`, `truncated`, `route_only` and `error`; with `--no-triage` the Jev call is not made. Triage is route only, so the reviewers run whatever the decision says, and a failed Jev call (a missing `TYPESAFE_API_KEY` included) does not change the exit code. `judge_gate` holds `status` (`ok`, `jev_error`, `not_run` or `disabled` for `--no-judge-gate`), `ref`, `decision`, `probabilities`, `truncated`, `path`, `verdict`, `judge_cost_estimate_usd` and `error`. When the gate skips the judge, `judge` and `weighted_score` are null, `ship` follows the gate's verdict, and `judge_gate.path` is `skipped_judge`; otherwise the judge runs as before and `path` is `escalated`. `--no-judge-gate` always runs the judge. The cost of triage and of the gate is in `total_cost_usd` and counts against the budget. Every run of the panel, triage and the judge included, is charged to one `Budget` of `--budget-usd` dollars (default `DEFAULT_BUDGET_USD`, 5.0); the first run to pass it ends the panel with `BudgetExceeded`, exit 1, and the message carries the spend so far. `tests/pipelines/golden/review-panel.json` is a normalised example.

`factory triage <repo_path> --base <ref> --head <ref> [--json] [--allow-unpublished]` runs only the triage of the panel: `diff-triage@^0.2` on the diff between the refs, then `decide`. Under `--json`, stdout carries one object with `pipeline_run_id`, `base`, `head` and the keys of the `triage` entry above, and the text report goes to stderr. It exits 0 when the Jev call succeeded and 1 when it failed (status `jev_error`, with the escalation printed), 2 for usage, and 3 for an unknown agent, repository or ref or an unpublished `diff-triage`. It needs `TYPESAFE_API_KEY` and not `ANTHROPIC_API_KEY`.

`factory fix <repo_path> --finding <json|@file> [--finding-index N] [--head <ref>] [--out-dir DIR] [--work-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]` runs the fix loop of `pipelines/fix_loop.py` on one finding: `test-writer@^0.1` writes tests for it, `fixer@^0.1` changes the code behind its action guard, the whole suite runs in the sandbox, and the panel of `factory review` reviews the change, all under one `pipeline_run_id` and one budget. `--finding` is a `Finding`'s JSON or a `factory review --json` document, whose judge's merged finding number `--finding-index` (default 0) is taken. The source repository must have nothing uncommitted and is never changed; the agents work in a clone under `--work-dir` (default: the system temporary directory), deleted at the end. Once the panel has run, the report goes to `<out-dir>/<pipeline_run_id>.json` (default `data/fixes`), and the patch to `<pipeline_run_id>.patch` beside it only when the panel approves. Under `--json`, stdout carries the report: `pipeline_run_id`, `repo_path`, `head`, `finding`, `test_writer` and `fixer` (`ref` and `output`), `files_changed` (each `path`, `action` and `by`), `suite` (`argv`, `exit_code`, `passed`, `seconds` and `output_tail`), `panel` (the `factory review` document), `ship`, `patch_path`, `report_path`, `total_cost_usd` and `duration_ms`. It exits 0 when the panel approves, 1 when it does not or when the loop fails (`TestFileWriteError`, `SuiteFailedError`, `FixError` when nothing changed, `BudgetExceeded`, or a `ToolExecutionError` from git on the clone, such as a clone over the 30 s limit or one that cannot be cleaned), 2 for usage or uncommitted changes, 3 for an unknown agent, repository or ref, and 4 for a missing `ANTHROPIC_API_KEY`, Docker or sandbox image. [fix-loop.md](fix-loop.md) describes the flow.

`scripts/check_version_bump.py [base]` is the guard behind the rule in "Agent address". It compares the working tree with the merge base of `base` (default `origin/main`) and exits 1, naming each agent, when its `system.md`, its `schemas.py` or the `questions` block of its `agent.yaml` changed while `version` did not. An agent directory that is new since the merge base needs no bump. It exits 2 when it cannot run, for example for an unknown base. It runs as the `version-bump` pre-commit hook, and CI runs it as `uv run python scripts/check_version_bump.py origin/main`. Edits to `registry/common_schemas.py` are not covered, because that module belongs to no single agent.

`factory cost --since <date> [--by agent|pipeline] [--json]` prints what the runs in the trace store cost from `--since` on. `--since` is a date or an ISO 8601 datetime, taken as UTC when it has no offset, and is required; a value that does not parse is a usage error. Runs are grouped by agent (`--by agent`, the default) or by pipeline run (`--by pipeline`), and split into `other`, `eval` and `publish` buckets by the row's tag. Under `--json`, stdout carries `since`, `by`, `total_runs`, `total_cost_usd` and `buckets`; each of the three buckets has `runs`, `cost_usd` and `groups`, a list of `{agent or pipeline_run_id, runs, cost_usd}` in which a run outside any pipeline is `(none)`. The text output has one table per non-empty bucket. `tests/runtime/golden/` holds the text and by-pipeline JSON examples, and `tests/golden/cost.json` pins the `--json` document with the other commands.

`factory cost --by kind` groups the same runs by runner kind, `claude` for an LLM run and `jev` for a Jev run, under the key `kind`; `tests/golden/cost-by-kind.json` and `tests/runtime/golden/cost-by-kind.json` pin it. `scripts/triage_saving.py` measures what diff-triage would save: it runs the review panel over every fixture twice, with triage and as `--no-triage` would, with the judge gate off in both, and reads the cost of each run back from the trace by pipeline run id. Its pipeline run ids start with `triage-saving-`, then a token per invocation, the fixture name and `triage` or `no-triage`. The projected saving is the reviewers' and the judge's cost in the with-triage runs of the fixtures the skip gate would skip, less the triage agent's cost over every fixture. The script exits 1 when a would-skip fixture has a `major` finding or worse, a run fails, or `--max-usd` stops it, and like `scripts/shadow_report.py` it never prints a diff, a fixture's file name or a finding's text ([jev-calibration.md](jev-calibration.md)).

`tests/test_golden_cli.py` pins the `--json` document of `list`, `eval`, `publish-check`, `review`, `triage`, `fix` and `cost` against files in `tests/golden/`, with the per-run fields (`duration_ms`, `git_sha`, `pipeline_run_id`, `eval_run_id`) replaced by fixed values. `pytest --update-golden` rewrites those files instead of comparing; read the diff before committing it. A new command gets one test and one file, as `tests/golden/README.md` describes. CI (`.github/workflows/ci.yml`) runs `pytest -m "not live and not live_jev" --cov --cov-fail-under=85`; the floor lives in the workflow command, not in `addopts`, so a local `pytest --cov` never fails on it. `.github/workflows/live.yml` runs the `live` and `live_jev` tests, by hand only (`gh workflow run live.yml`), with the keys from the repository secrets `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY`.

## Exit codes

`factory.exit.ExitCode` is the single source of truth. The numbers match agb-tools, so the `agb factory` wrapper (#30) passes them through unchanged. agb-tools also defines 75 for a conflict; the factory has no such case and does not use it.

| Code | Member | When |
| --- | --- | --- |
| 0 | `OK` | Success |
| 1 | `ERROR` | Failure, or a negative result |
| 2 | `USAGE` | Bad arguments or flags, including a missing or unknown subcommand |
| 3 | `NOT_FOUND` | The agent, version, run, fixture or file asked for does not exist |
| 4 | `AUTH_OR_CONFIG` | Missing or invalid key, config or required tool, and API authentication failures |

`main()` returns an `ExitCode`. Never call `sys.exit` with a bare number.

```python
def main(argv: Sequence[str] | None = None) -> int:
    ...
    return ExitCode.NOT_FOUND
```

## Errors

A failure the user or agent should see is a `FactoryError` with a one-line `message` and an optional `hint`. Subclass the base that matches the failure, and the exit code follows:

| Base | Exit |
| --- | --- |
| `FactoryError` | 1 |
| `UsageError` | 2 |
| `NotFoundError` | 3 |
| `AuthOrConfigError` | 4 |

Later typed errors subclass one of these. A resolver miss (`AgentNotFound`, `VersionNotFound`) is a `NotFoundError`; a missing key is an `AuthOrConfigError`; `AgentError`, `JevError`, `GuardError` and `BudgetExceeded` are `FactoryError`s; `TokenLimitError` is an `AgentError`. `BudgetExceeded` lives in `runtime/budget.py`. `JevError` lives in `runtime/jev_runner.py` and exits 1.

`GuardError` lives in `runtime/guard.py` and exits 1. It is raised for a third denial in one run, or for a guard that cannot answer, and it is not an `AgentError`.

`runtime/errors.py` holds the failures of an LLM run. Each is an `AgentError` (exit 1) with the agent's `name@version` address as `agent` and the messages so far as `transcript`, in the shape the Messages API takes, thinking signatures kept:

| Error | Raised when | Extra attribute |
| --- | --- | --- |
| `OutputValidationError` | The answer failed the output model twice | |
| `RefusalError` | The model stopped with `refusal` | `category` |
| `OutputTruncatedError` | A reply hit `max_tokens` or the context window | `stop_reason` |
| `TurnLimitError` | The model asked for more than `limits.max_tool_turns` tool turns | `limit` |
| `AgentTimeoutError` | A request timed out, or the run passed `limits.timeout_s` | `timeout_s` |
| `ToolCrashError` | A tool or hook raised something other than a `ToolError` or `FactoryError` | `tool` |
| `ModelAPIError` | Any other API error, an unexpected stop reason, or no final answer | |

The CLI's root handler in `main()` maps any exception raised by a command with `exit_code()` and, under `--json`, renders it with `error_envelope()`; without `--json` it prints the message and, if there is one, `hint: <hint>` to stderr. An invalid manifest, or a file it points to, raises `ManifestError`, an `AuthOrConfigError` (exit 4) whose one-line message starts with the manifest path and names the field.

`known_exceptions()` lists the foreign types that have a code:

- `argparse.ArgumentError` exits 2.
- `anthropic.AuthenticationError` and `anthropic.PermissionDeniedError` exit 4.
- Everything else exits 1.

Some types are deliberately left unmapped:

- `FileNotFoundError`, because a missing executable raises it too, and that is exit 4, not 3.
- `anthropic.NotFoundError`, which can mean an unknown model id or something else.
- Pydantic and YAML errors.

Code that understands such a failure catches it and raises the right `FactoryError`.

```python
class AgentNotFound(NotFoundError):
    """No manifest matches the requested name and range."""


raise AgentNotFound("no version of code-reviewer matches ^3.0", hint="available: 1.2.0, 1.3.0")
```
