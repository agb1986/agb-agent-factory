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
| Work items, states and the orchestrator | [Work items](#work-items) | `runtime/work_items.py` (#82) |
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
| `pipelines/` | `review_panel.py`, `fix_loop.py`, and `file_policy.py`, the host-side check of an agent's `files` block |
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
| `tools.web.allow_domains` | Optional. Bare lowercase hosts the web tools may reach; a listed domain allows its subdomains. Empty or absent means the agent gets no web tools; see [Tools](#tools) |
| `limits.max_tool_turns` | Most tool-use turns in one run. A turn is one assistant message that stops with `tool_use`, however many calls it holds |
| `limits.timeout_s` | Wall-clock limit for one run, in seconds, and the timeout of each request |
| `limits.max_output_tokens` | `max_tokens` for each request |
| `limits.max_retries` | Optional, default 2. How many times one model request that failed with a 429, a 529, a 5xx or a connection error is sent again; 0 turns retrying off. It is separate from the single validation retry. See [Request fields](#request-fields) |
| `evals.min_pass_rate` | Eval pass rate the publish gate requires, from 0 to 1 |
| `guard` | Optional. The action-guard agent that must allow each of this agent's tool calls that is not read-only, as `name@^MAJOR.MINOR`, such as `action-guard@^0.1`. Absent means no guard; see [Tools](#tools) |
| `team` | Optional. The agent's team, a label that never reaches the model; see [Teams](#teams) |
| `files.allow`, `files.deny` | Optional. Glob patterns, relative to the repository root, for the paths the agent's writes may and may not touch; see [File policy](#file-policy) |

Unknown keys are an error, so a typo never falls back to a default. Later tickets add fields and document them here.

Two of those fields exist now and are only declared: `sandbox` (`false` by default, `true` or `spike`) says the agent may be given the sandbox-only tools and names the profile they run under: `true` is the offline sandbox over the work tree, and `spike` is the networked one over a fresh empty scratch directory with no repository, described under [Tools](#tools), and `limits.max_tokens_total` (an integer, optional) is the most tokens one run may use. The runner enforces `max_tokens_total` after every model response: a run whose tokens pass it raises `TokenLimitError` (see [Request fields](#request-fields)). `sandbox: true` is the manifest's half of the gate described under [Tools](#tools).

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
| `act_on_decision` | Optional, default `false`. When true, a pipeline that allows it acts on the `Decision`: a skip skips the work and a route narrows it. When false, the decision is logged and everything runs |

Gate rules read the question's generated output:

| Rule | Reads | Valid on | `value` |
| --- | --- | --- | --- |
| `p_above`, `p_below` | `p` for a noul; `probs[option]` for a choice or score, with an extra `option` key | all types | a probability |
| `confidence_below` | `confidence` | choice, score; noul outputs carry no confidence | a probability |
| `answer_in` | `answer` for a choice, `level` for a score | choice, score | a list of options or levels |

The generated output models are: noul `{p: float}`; choice `{answer: str, probs: dict[str, float], confidence: float}`; score `{value: float, level: str, probs: dict[str, float], confidence: float}`. `JevOutput` has one field per question id. A truncated input always forces review, whatever the gates say.

A Jev manifest pins a versioned model id such as `jev-1.14.0`, never the rolling aliases `jev-latest` or `jev-preview`, because gate thresholds are tuned against one version. `GET /v1/models` lists only the aliases, so a pinned id is verified live by checking that the response's `model` field equals it. The spike notes in [jev-sdk.md](jev-sdk.md) (#15) give the wire format behind these output models.

`registry/jev_spec.py` validates all of this when the manifest loads and reports every problem at once, naming the gate or routing entry by index. Question ids and gate names are lowercase `snake_case`, and an id must also be usable as an output field, so Python keywords and names starting with `model_` are refused. A `render` template may use only plain field names such as `{diff}`: no `{}`, `{0}`, attribute or index access, or fields nested in a format spec, and it must use at least one. A `p_above` or `p_below` gate on a choice or score needs `option`, which must be one of its options or levels; on a noul it must not set `option`. `answer_in` values must be real options or levels. A gate may name another gate in `unless`, and then it does not fire when that gate fires; the named gate must exist, must not be the gate itself and must have no `unless` of its own. A `routing` entry's `when` is a gate name, or `question=answer` for a choice or score question and one of its options or levels, and each `when` appears once. `JevSpec.output_class` builds the output model once, a `JevOutput` subclass named after the agent (`DiffTriageOutput`) with one field per question id. A choice or score field also checks that `answer` or `level` is one of the question's answers and that `probs` has exactly those keys. A score's `value` is the API's expected level mapped linearly onto `range`: with `n` levels and expected level `score` (0 to `n - 1`), `value = min + score / (n - 1) * (max - min)`. A `score` outside 0 to `n - 1` is a `JevError`, so `value` always lies within `range`.

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

Any change to a prompt, schema or question bumps the version in the same commit; `scripts/check_version_bump.py` enforces it (see "CLI shape"). A change that breaks the input or output model bumps the major. A change that alters behaviour (prompt, questions, model, effort, tools, limits, gates, a compatible schema addition) bumps the minor. A change that never reaches the model (description, eval cases) bumps the patch. Setting or changing `team` or the `files` block bumps nothing: the team is a label, and the file policy is enforced by the pipeline around the agent, not by the agent.

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
- Tools are read-only. A tool that writes may be allowed only for an agent that runs inside the sandbox (#24), behind the action guard (#23). A tool that writes outside the sandbox is never given to a model; pipeline code calls it, under [Outside writes](#outside-writes).
- A tool reads only inside the repository root it is given. A path that escapes the root, whether through `..`, an absolute path or a symlink, is rejected with a typed error. Subprocesses run from an argument list, never through a shell, and output is capped with a truncation marker.

A tool implementation is a function `impl(repo_path, **arguments, max_bytes=MAX_OUTPUT_BYTES) -> str`. The definition in `TOOLS` lists only the arguments the model supplies; the runner passes `repo_path` itself and never forwards a model-supplied `max_bytes`. `repo_path` must be the top of a git work tree. A path is resolved against the real disk and must stay inside it, but an absolute or `..` path that ends up inside is accepted. Output beyond `MAX_OUTPUT_BYTES` (64 KiB) is cut on a character boundary and ends with one line, `[truncated: N bytes dropped, limit M bytes]`. Every failure is a `ToolError` subclass from `tools/_common.py`, so the runner can turn one clause into an error tool result, and its exit code follows the usual bases (`PathEscapeError` and `ToolInputError` 2, `ToolNotFoundError` and `UnknownToolError` 3, a missing `git` or `rg` 4). `tools.dotted_name(wire)` raises `UnknownToolError` for a name the model made up.

The runner calls a hook around every tool execution. The protocol is `ToolHook` in `runtime/hooks.py`, and `run(..., hook=...)` takes a new one per run; the default, `AllowAll`, allows everything.

- For each call the runner builds a `ToolCall` with the dotted `name`, the model's `arguments` and the calling `agent` address, and asks `before_tool(call)`. It returns `ToolDecision.allow()` or `ToolDecision.deny(message)`.
- A denied call never runs. Its message goes back to the model as an error tool result, and `after_tool` is not called. A denial never ends a run by itself.
- An allowed call has its arguments checked against the tool's schema and then runs. The hook sees `after_tool(call, ToolResult(content, is_error))` with what went back to the model.
- A `ToolError` from a tool goes back to the model as an error result, `message (hint: ...)`. `ToolUnavailableError` (a missing `git`, `rg` or `bandit`) ends the run instead, since the model cannot work around it.
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

The registry is discovered, not written by hand. Every public module in `tools/` (`git.py`, `fs.py`, `web.py`, ...) ends with a module-level `TOOLS` tuple of `tools._common.ToolSpec` values, one per tool: its dotted `name`, the `description` and `input_schema` the model sees, the `impl` that follows the registry contract, and three flags. `tools.registry` imports every module in name order, collects the tuples in declaration order and builds `TOOLS`, `READ_ONLY`, `SANDBOX_ONLY`, `SANDBOX_DENIED` and `NEEDS_WEB_DOMAINS` from them; a duplicate name or a value that is not a `ToolSpec` fails at import. A new tool is one `ToolSpec` appended to its module's tuple, a test of its own and a changelog fragment; `tools/registry.py` and `tests/tools/test_registry.py` do not change, which is what lets several tool pull requests merge without conflicts. A new module is a new file under `tools/` with its own `TOOLS` tuple; the registry finds it.

`tools.registry.READ_ONLY` is the set of tools that cannot change anything: every spec without `writes=True`. A tool that is not in it, including a name the registry does not know, is treated as one that changes something. A tool is declared without `writes` only if it reads and nothing else.

`memory.search(query, limit)` (#89) answers the curation agent that sp.md proposes, together with the existing `agb mem save` writes, so the factory builds no RAG or vector store of its own. It reads the machine's memory by running `agb --json mem search --backend all --limit N -- QUERY`, which merges claude-mem and OpenViking, and returns one entry per match as `[backend] ref | timestamp | title` plus an indented snippet line. `backend` is the source and `ref` (`claude-mem:<observation id>` or a `viking://` URI) is the stable reference an agent cites. `limit` is 1 to 25 (default 10) and the output is capped like any tool's. Writing to memory stays with `agb mem save`, which is not a tool. `memory.search` is in `READ_ONLY` but reaches the host, so it is declared with `reaches_out=True` and the registry refuses it to an agent with `sandbox: true` (`SANDBOX_DENIED`, below). A backend that is down raises `ToolUnavailableError`, which ends the run.

An agent whose manifest sets `guard` gets an action guard. `runtime/guard.py` holds `GuardHook`, a `ToolHook` that `run` installs when `spec.guard` is set, in front of any `hook` the caller passed: the caller's hook decides first, and only a call it allows goes to the guard. For a call to a tool in `READ_ONLY` the hook allows and writes nothing. For any other call it renders the tool name, the arguments, the worktree root (`repo_path`, resolved) and a task summary (`name@version: description` of the guarded agent) as the guard agent's input, runs the guard through `runtime.runner.run`, and acts on its `allow` and `reason`. A denial goes back to the model as the error tool result `<tool> refused by the action guard: <reason>`. The third denial in one run raises `GuardError` instead (a `FactoryError`, exit 1), which `run` re-raises unchanged and records in the guarded run's own trace row. Allowed calls do not reset the count. A guard run that fails with an `AgentError` also raises `GuardError`: a call is never allowed on a guess. A missing API key or any other `FactoryError` from the guard run propagates as it is.

Every guard decision is one trace row, whether it allows or refuses. Its `kind` and `agent_ref` are the guard agent's, and `decision_json` is `{"parent_run_id", "allow", "reason"}`, `parent_run_id` being the `run_id` of the run whose call was judged. For an LLM guard the row is the guard run's own row, so it carries the tokens and the cost. The guard agent has no tools, so it never recurses.

The guard comes in two forms, chosen by the `kind` of the agent that `guard` names. `kind: llm` is the form that ships, `action-guard@0.1` on `claude-haiku-5-5` at effort `low`. `kind: jev` would be a noul answering "is this tool call safe inside the sandbox?" and applies once Jev is a go; it is not wired, and `build_guard_hook` raises `UsageError` for it, since turning a probability into allow or deny needs a threshold, and thresholds live in `runtime/gates.py`.

The sandbox-only tools are `fs.write_file` and `shell.run`, declared in `tools/sandbox.py` with `writes=True`, which puts them in `SANDBOX_ONLY`; `tool_defs(allow, deny, sandbox=...)` raises `ToolListError` for either when the manifest does not say `sandbox: true`, and the runner passes the flag. The other way round, a tool declared with `reaches_out=True` (the Sentry, memory and web tools) is in `SANDBOX_DENIED` and `tool_defs` refuses it when the manifest says `sandbox: true`, and one with `needs_web_domains=True` (the web tools) is refused without a `tools.web.allow_domains` list. `fs.write_file(path, content)` replaces or creates one file inside the repository root, refuses anything under `.git` and content over 256 KiB. `shell.run(argv)` takes an argument list and starts only `pytest`, `python`, `uv` or a read-only `git` subcommand, as a bare name, inside a throwaway container with no network that sees only the work tree (option (a) of `docs/decisions/0001-fixer-runtime.md`). A non-zero exit is a normal result, reported as `exit code N` and the output; a command past 120 s is killed and reports 124. Under `sandbox: spike` (#106) the same two tools run in a second profile, `tools.sandbox.run_spike`: the runtime makes a fresh empty `factory-spike-*` directory for the run and removes it afterwards, `fs.*` read and write inside it, and `shell.run` mounts it at `/work` in a container on the default bridge network that may also start `pip`, with a 90 s limit and the offline profile's CPU, memory and process limits. No source repository is mounted. The spike profile also allows the web tools, which still need `tools.web.allow_domains`, and the action guard is told the profile (`action-guard@0.2`). Its network is not filtered at the container level; the guard is what refuses private and metadata addresses. The image is built once, with the network, by `docker build -t factory-sandbox:0.1 -f tools/sandbox.Dockerfile tools`; a missing Docker or image raises `ToolUnavailableError` and ends the run. `fs.list_files` and `fs.read_file` are read-only and read the work tree as it is on disk, edits included, so an agent that writes can read back what it wrote. Both are in `READ_ONLY`, so the action guard lets them through; every tool is in exactly one of `READ_ONLY`, `SANDBOX_ONLY` and `OUTSIDE_WRITES`, decided by its `writes` and `outside_write` flags.

The fix loop (`pipelines/fix_loop.py`, #24) adds a rule the tools do not enforce: only the test-writer changes tests. The rule is the fixer's [file policy](#file-policy): its manifest denies the test layout that `evals.graders.sandbox_tests.is_test_file` matches, pytest's configuration at the root of the clone (`pyproject.toml`, `setup.cfg`, `pytest.ini` and the like), and root modules named like one the runner imports (best effort; the panel is the real check). On top of the manifest the fix loop protects, for that run, every path the test-writer changed, as its commit shows and not as it reports. `FilePolicyGuard` is the fixer's `hook`, so it decides before the action guard, and checks the path as written and again with symlinks resolved; after the fixer's run, `check_changed` on the host refuses the same paths when they changed by another route, such as a script started through `shell.run`. A refusal ends the run rather than going back to the model as a denial, because a fixer that wants to change a test either thinks the test is wrong or is working around it, and both need a person. It writes one trace row, `tag` `fix:test_write_refused` (`TEST_WRITE_REFUSED_TAG`), `kind` `llm`, `agent_ref` the fixer, `cost_usd` 0, `error` the message and `decision_json` `{"allow": false, "path", "reason", "check"}`, `check` being `hook` or `post-check`, and raises `TestFileWriteError`, a `FactoryError` (exit 1). The agents work in a clone (`git clone --no-hardlinks`, `pipelines/sandbox.py`), not a `git worktree`: a worktree's commits and admin entries land in the source repository's `.git`, which the fix loop never writes, and its `.git` file points outside the work tree, where the sandbox, which mounts only the work tree and its `.git` read-only, cannot follow. Before the suite the clone is reset to the fixer's commit with its untracked and ignored files deleted (`pipelines.sandbox.clean`), so the suite runs over the commits alone. The suite gets 600 s (`SUITE_TIMEOUT_S`) instead of a tool call's 120 s, and `FACTORY_SANDBOX_IMAGE` names another image than `factory-sandbox:0.1`, such as one holding a target repository's test dependencies.

The build loop (`pipelines/build_loop.py`, #87) runs its coder (the feature-coder, the refactorer or the documentation agent) behind the same two checks over its manifest's `files` policy alone, in the same kind of clone, with the suite under the same 600 s limit. A refusal ends the run in the same way: one trace row, `tag` `build:file_write_refused` (`FILE_WRITE_REFUSED_TAG`), `kind` `llm`, `agent_ref` the coder, `cost_usd` 0, `error` the message and `decision_json` `{"allow": false, "path", "reason", "check"}`, and it raises `FileWriteRefusedError`, a `BuildError` (exit 1). To open the pull request, the loop fetches the coder's last commit from the clone into the source repository's object store with no ref, and `github.create_pr` pushes it from there, since the target is read from the source repository's `origin`. A file policy can only allow or deny a whole path, so a coder that may change part of a file needs a check after the commit as well: the documentation agent's policy allows Markdown, `docs/**` and source files in the languages `pipelines.docs_check.COMMENT_SYNTAX` knows, and the loop's docs check (`DocsCheck`, #97) then refuses a round that changed a Python file outside its docstrings and comments, another source file outside its whole-line comments, a file of an unknown type, or that deleted a file or created a source file. Keep that allow list in step with `DOCS_GLOBS` and `COMMENT_SYNTAX`. A dry run writes the change as `<pipeline_run_id>.patch` in `--out-dir` instead of opening the pull request. See `docs/build-loop.md`.

`deps.graph` and `deps.impact` (`tools/deps.py`, #88) are read-only and in `READ_ONLY`. `deps.graph` reads the Python, TypeScript and JavaScript files that `git ls-files` lists as tracked or new and not ignored, and returns the modules, named by their path from the root, and the import edges between them. Python is parsed with `ast`; TypeScript and JavaScript are matched with regular expressions after their comments are blanked out, so text that looks like an import inside a string can be misread. `deps.impact(paths)` returns every module that imports one of the paths, directly or transitively, with the depth of the shortest chain, 1 for a direct importer; the paths themselves are left out, and a cycle is walked once. The tools never guess. An import that names no single file is listed as unresolved with a reason: `dynamic` (`importlib.import_module`, `__import__`, `import()`, `require` of an expression), `alias` (a key of `compilerOptions.paths` in the root `tsconfig.json` or `jsconfig.json`, `@/`, `~`, `#`, or a bare specifier that starts with a top-level directory of the repository), `not_found`, `outside_root`, `absolute`, or `ambiguous` (a Python import that two source roots both satisfy). Imports of the standard library and installed packages are external and are not edges. An import of a barrel file (an `__init__.py` or an `index` file) is an edge to the barrel; which re-exported module a name comes from is not resolved, but the barrel's own re-exports are edges, so the impact set still reaches through it. Symlinks are skipped, and so are files over 1 MiB and Python files that do not parse. Because unresolved imports are not followed, `deps.impact` ends with a note when the repository has any: the set may then be incomplete.

`sast.scan(base_ref, head_ref, path=None)` (`tools/sast.py`, #101) is read-only, sets no flag and is in `READ_ONLY`. It runs bandit 1.9.4, a core dependency pinned in `uv.lock`, with a fixed ruleset (`tools.sast.RULESET`, every check of that version except `B101`, the `assert` check) passed as `--tests`, so a plugin or a config file in the repository under review cannot add or remove a rule. It scans the files that `git diff --diff-filter=ACMT base head` lists, optionally under `path`, as their blobs at `head`, copied into a temporary directory under neutral names; the work tree is never read, `# nosec` comments are ignored, and symlinks, files over 1 MiB and files that are not `.py` are listed as `not scanned: path (reason)` rather than scanned, as are files bandit could not parse. The output is a header `bandit: N files scanned, M findings`, then one `path:line: RULE name: message (severity S, confidence C)` line per finding in path and line order with head-revision line numbers, then the `not scanned` lines, capped like any tool's. A missing `bandit` raises `ToolUnavailableError`, which ends the run; a scanner that exits non-zero or prints no JSON object raises `ToolExecutionError`. A scanner finding is a lead, not a review finding: an agent that holds `sast.scan` must verify each one against the code and write its own finding, or drop it. Bandit rather than semgrep because it is one small wheel with its rules built in and needs no network or rule registry at scan time, while semgrep is a large binary wheel whose useful rules come from a registry under their own licence; the cost is that bandit reads Python only.

### Outside writes

An outside write changes something a person reads outside the sandbox: a pull request, a review comment, an issue. The rules below are decided in `docs/decisions/0003-outside-writes.md` (#83, accepted 2026-11-20) and bind every tool that does one.

- Every outside write is its own named tool, `<group>.<verb>`, in the `github` or `notify` group. There is no generic write tool.
- It is denied by default. A pipeline may make the write only on behalf of an agent whose resolved manifest lists the tool in `tools.allow`; for any other agent the runtime refuses. A command-line flag may ask for a write the manifest already allows (`--comment`) or turn writes into a plan (`--dry-run`). No flag grants one.
- No model is given an outside write. The `ToolSpec` is declared with `outside_write=True`, which requires `reaches_out=True` and forbids `writes=True`, so the tool is in `OUTSIDE_WRITES` and `SANDBOX_DENIED` and never in `READ_ONLY`. `tool_defs` checks the name like any other and leaves it out of the definitions sent to the model, so the rule that a model's tools are read-only outside the sandbox holds without an exception.
- Pipeline code makes the write through one function, `runtime.outside.write(name, arguments, *, agent, context)`, where `agent` is the address of the agent the write is made for and `context` is an `OutsideContext`: the target, the `pipeline_run_id`, the `dry_run` flag, the trace store and the authorising agent's `run_id` in this pipeline, if it ran. The impl of an outside write takes the context in place of `repo_path`, and the runner never calls it.
- Every call passes an outside-write guard before it runs. The guard is a `ToolHook` of the action guard's shape, so `before_tool(call)` returns `ToolDecision.allow()` or `ToolDecision.deny(reason)`, but its checks are code, not an LLM: the tool is in `OUTSIDE_WRITES`, the authorising agent lists it, the target is the pipeline's own, and no earlier call of the same tool on the same target was made in this pipeline run. A denial raises `OutsideWriteRefused`, a `FactoryError` (exit 1), and ends the stage.
- The target is the repository the pipeline was given and nothing else. It is the `owner/name` on github.com of the `origin` remote of that repository, read once when the pipeline starts and compared without regard to case. There is no flag to name another. An `origin` that is not on github.com is a `UsageError` (exit 2). A tool handed another repository, a pull request or issue whose base repository is another, or a fork refuses with `OutsideWriteRefused`; the tool checks this itself as well as the guard, so a bug in one alone cannot write elsewhere.
- `--dry-run` belongs to the pipeline command and reaches every outside write in it. The guard still runs, read-only tools such as `github.read_pr` still run, and the tool builds its complete request but does not send it. It returns what it would have done, and the trace row says `dry_run`.
- Every body a write posts ends with the marker line `<!-- factory:<tool> <key> pipeline_run_id=<id> -->`, `key` being `pr-<N>`, `issue-<N>`, `ticket-<id>` or `branch-<name>`. A rerun finds its earlier post by the prefix `<!-- factory:<tool> <key> ` and edits it instead of posting again.
- `github.create_pr` pushes one new branch, `factory/<pipeline_run_id>`, to the target and opens a pull request from it to the default branch. It never pushes to an existing branch, never force-pushes and never pushes to a fork. It may apply labels that already exist on the target; it never creates one.
- Deployment is out of scope. No tool merges, closes, deletes, releases, tags or deploys, and no `deploy.` group exists, until a separate decision record says otherwise.

The permitted outside writes:

| Tool | What it writes | Listed by | Pipeline |
| --- | --- | --- | --- |
| `github.create_pr` | A `factory/` branch and a pull request from it, with existing labels (`hotfix` for #109) | `judge` | `factory build` (#87), `factory hotfix` (#109) |
| `github.comment_pr` | One review on a pull request, inline comments by path and head-revision line or one summary comment; a rerun edits it | `judge` | `factory review --pr N --comment` (#100) |
| `notify.issue` | An issue carrying an escalation and its summary, or a comment on the one already open for the ticket | `escalation-summary` | The escalation path (#93, #95) |

`github.read_pr` and `github.read_issue` live in `tools/github.py` beside them but only read: they are declared with `reaches_out=True` alone, are in `READ_ONLY`, and a model may hold them. A tool that is not in this table does not write outside the sandbox.

Every attempted outside write is one trace row, whatever its outcome, with no new column and no new `kind`:

| Field | Value |
| --- | --- |
| `kind` | `llm` |
| `agent_ref` | the authorising agent's address, such as `judge@2.1.0` |
| `tag` | `outside:<tool>`, such as `outside:github.create_pr` |
| `pipeline_run_id` | the pipeline's run id |
| `parent_run_id` | the authorising agent's `run_id` in this pipeline, or null when it did not run |
| `input_json` | the arguments the pipeline passed |
| `decision_json` | `{"tool", "target": {"repository", "pr", "issue", "branch", "ticket"}, "outcome", "allow", "reason", "dry_run", "url"}` |
| `output_json` | `{"action": "create" \| "edit", "url", "body"}`, `url` null in a dry run; null when refused |
| `error` | `<ErrorClass>: <message>` when the outcome is `refused` or `failed`, else null |
| `cost_usd` | `0.0`, with the token fields null |

`outcome` is one of `written`, `dry_run`, `refused` and `failed`. `target` keeps the keys that apply to the tool and sets the others to null. `tag LIKE 'outside:%'` selects every outside write in the trace store.

The GitHub tools (`tools/github.py`, #84) and `runtime/outside.py` settle the points the decision record leaves open. `OutsideContext.for_repository(repo_path, ...)` reads the target from `origin` as written in the repository's config, not after `insteadOf` rewrites, and the guard reads it again before each call, so a remote changed after the pipeline started is refused. `refused` covers arguments that do not fit the tool as well as the guard's and the tool's own refusals, all before anything is sent; `failed` is any later error, after which part of the write may have reached GitHub. A `failed` call counts for the once rule like `written` and `dry_run`, a `refused` one does not, and the rule compares the target's pull request, issue or branch, so one pipeline run may comment on two pull requests but not twice on one. `github.comment_pr` finds its earlier post by the marker prefix among the posts of the login `GH_TOKEN` belongs to: a rerun edits the review's body and its inline comments in place, marks an earlier inline comment the new run did not report as `Not reported by the latest run.`, and lists new ones in the body, because GitHub does not let a review gain comments after it is submitted. `github.create_pr` pushes with `git push --no-verify` to `https://github.com/<owner>/<name>.git` with `gh auth git-credential` as the only credential helper, so the push authenticates with `GH_TOKEN` too and runs no hooks.

`notify.issue` (`tools/notify.py`, #95) adds `ticket` to the target's keys, beside `pr`, `issue` and `branch`, and the once rule compares it too, so one pipeline run posts at most once per ticket: a second escalation of the same item in one run is refused, and its stop is still recorded. The tool's own result adds `state` to the target, the issue's `open` or `closed`. The marker key is `ticket-<id>`, and each post's marker goes on with ` escalation=<esc-n>` before `pipeline_run_id=`, so the ticket's prefix finds the issue and the escalation's finds the post. The details are under [Posting to GitHub](#posting-to-github).

### File policy

An agent that writes to a repository may declare, in its manifest's `files` block, the paths its writes may and may not touch (`registry.spec.FilesSpec`, #86). Both lists hold glob patterns relative to the repository root, matched with `PurePosixPath.full_match`: `*` matches within one path segment and `**` any number of whole segments. A path may be written when `allow` is empty or one of its patterns matches, and no `deny` pattern matches; `deny` wins. A pattern that is absolute, climbs out with `..`, is listed twice or is in both lists fails the manifest.

```yaml
files:
  deny:
    - "**/tests/**"
    - pyproject.toml
```

The block is enforced on the host, never in the sandbox where the agent could change the check, by the pipeline that runs the agent, with `pipelines/file_policy.py`. `FilePolicyGuard` is the agent's `hook`: it refuses an `fs.write_file` to a denied path, as written and with symlinks resolved, before the action guard sees the call. `check_changed`, called once the run is over, refuses a denied path that changed by another route, such as a script run through `shell.run`. `FilePolicy` lets a pipeline protect more paths for one run, each with its own reason, as the fix loop protects what the test-writer changed. A refusal ends the run through the pipeline's `Refuse` callback, which writes the trace row and returns the error to raise; the fix loop's and the build loop's are described under [Tools](#tools).

The fixer's policy denies tests, pytest's root configuration and the modules that would shadow pytest. The feature-coder's denies version control, CI, the same pytest configuration and the same shadow modules, and allows tests and code alike. A pipeline that does not run an agent's policy leaves it unenforced: the eval runner does not run it. The build loop (#87) runs the feature-coder's. The block reaches no model and bumps no version.

## Fixtures

A fixture is a directory `fixtures/src/<name>/` holding a `base/` tree, a `head/` tree and a `defects.yaml`; hand-built ones live in `fixtures/handmade/<name>/` in the same shape. `uv run python -m fixtures.build [name ...]` turns each into a git repository under `fixtures/repos/<name>/` (gitignored) with branches `base` and `head`, one commit each. Identity, dates and branch names are fixed and no hooks or git config apply, so the SHAs are reproducible; a rebuild deletes the old repository first. The directory name is the fixture's name and the handle an eval case uses, so a name is never renamed or reused. A new fixture only needs its directory: `discover` picks it up, and `ROOTS` in `fixtures/build.py` lists the source roots.

`defects.yaml` lists the planted defects: `id`, `file`, `line_start`, `line_end` (1-based, inclusive, in the head tree), `severity` (`info`, `minor`, `major` or `blocker`, as in a finding), `category` (a lowercase slug; so far `logic`, `style`, `security`, `performance`, `testing`, `hardcoded-secret`, `path-traversal`, `documentation`; a fixture planted for an agent that fixes its category strings in its prompt, such as security-reviewer, uses one of those strings), `title`, `description` and a `snippet` that must appear inside the window. The list is empty for a fixture a reviewer should pass without findings. The build fails when a window has drifted off its code. Source trees are linted and type-checked with the rest of the repository, so a planted defect must be one ruff and ty accept.

A reviewer that works on one kind of defect names the categories it may use in its own `system.md`; `Finding.category` stays a free string and gains no enum. The performance reviewer uses `algorithmic-complexity`, `n-plus-one`, `missing-index`, `unbounded-memory`, `blocking-call` and, when none of those fits, `performance`, so a fixture planted for it takes its `category` from that list (`performance` is also what the older `n-plus-one` fixture records). Because the reviewers run in the same eval harness, a perf fixture's defect is graded on file, line window and severity, not on the category string.

Two variants serve the agents that write tests and fix code. `off-by-one-red` is `off-by-one` plus a test that fails on `head` and passes on `base`, the input of the fixer; it is the one fixture whose own suite is red on `head` (`tests/fixtures/test_fixtures_build.py` expects that). `untested-change-covered` is `untested-change` with a passing test file, so the fixer has nothing to fix and `defects.yaml` has `defects: []`. `partly-covered` is green on `head` with some lines of `inventory/stock.py` untested, the input of the test-writer's cover mode. The fixtures have no `pyproject.toml`; their tests run with `python -m pytest -q` from the repository root.

`mixed-red` and `sql-injection-red` are further fixer fixtures of the same kind (#65): `mixed` and `sql-injection` plus a test in `head` that fails on the planted defect, so their own suites are red on `head` too (`FAILS_ON_HEAD`). They are separate fixtures because a failing test added to `mixed` or `sql-injection` would show up in the diff the reviewers read and give the defect away. Their `defects.yaml` repeats the entries of the fixture they copy. `sql-injection` and `path-traversal` have no `base` state in which `find_by_name` or `read` exists, so a test-writer case on them cannot set `passes_on_base`; it checks `new_tests: fail` only.

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

A payload value may be `{$fixture: diff}` or `{$fixture: files}`, with no other key. The eval runner replaces it with the diff of the built fixture from `base` to `head`, read through `show_diff` and cut at `MAX_OUTPUT_BYTES` as the review panel reads it, or with the files that diff names, one per line. Any other `$fixture` value is a `CaseFileError` at load. A Jev agent's case may also set `expected.answers`, a map from a choice question id to the option it must pick; a noul question has no option to expect, and an unknown question or an option the question does not offer is a `CaseFileError` before any case runs. `expected.decision` (`skip`, `review` or `escalate`) is checked against `runtime.gates.decide` on the answers, where `review` means neither skip nor escalate and an input cut by the runner or by `run_jev` decides as truncated; only a Jev agent with gates may set it. `expected.risky: true` marks a diff a reviewer must see; it needs `decision`, and the graders never see it. Answers and decision failures read like verdict failures and fail the case before any grader call. `agents/diff-triage/evals/cases.yaml` is the example.

For an agent whose cases set `decision`, the eval reports three metrics over the cases that ran and decided: `recall_risky`, the share of risky cases not skipped; `skip_rate`, the share skipped; and `escalation_rate`, the share escalated. Each is null when no case applies, which is always so for an LLM agent. When the manifest sets `evals.min_recall_risky` and some risky case decided, the eval also fails when `recall_risky` is below it, whatever the pass rate, and the table's summary line names both and ends `BELOW MINIMUM`.

The `sandbox_tests` grader (`evals/graders/sandbox_tests.py`) is for agents that write tests or code, which are graded on the work tree they leave and not on their answer alone. It needs `expected.verdict: null` and an `expected.run` block, and it never calls a model by itself. An agent with `sandbox: true` runs on a private copy of the case's fixture, so cases do not see each other's edits. `expected.run` may hold `new_tests` (`fail` or `pass`: pytest over the files in the answer's `test_files` exits 1, or 0), `passes_on_base` (also `pass` once the non-test files are put back to the fixture's `base` state, which shows the test fails for the defect and nothing else), `suite` (`pass`: the whole suite exits 0), and `production` and `tests` (`changed` or `unchanged`: whether any non-test or test file differs from the fixture's HEAD). `coverage` (`paths`, `min_gain`, `max_missing`) measures the line coverage of `paths` with `evals.coverage` twice, once with the files the agent changed put back to HEAD and once as it left them, and fails the case when fewer than `min_gain` lines became covered or more than `max_missing` are still missing; the test-writer's cover-mode cases use it. `public_api` (a list of `.py` paths) compares the public names and signatures of each file in the work tree with the fixture's HEAD by `ast`, with `evals.public_api`, and fails the case on any name added, removed or changed, or on a file deleted or no longer parsing; the refactorer's cases use it (#98). `doc_drift` (`paths`, the Python files the change touched, and `docs`, the globs of the docs files) runs the deterministic drift check of `evals/doc_drift.py`: every public function, class and method in `paths` has a docstring, every public top-level name is named in backticks in a docs file, no docs file names in backticks a lone identifier that was a public top-level name of a file in `paths` at the fixture's `base` or HEAD and is not one in the work tree, and every changed file passes the build loop's docs check rules (`pipelines.docs_check.check_change`) against HEAD. Its failures are reported, never raised. The documentation agent's cases use it (#97); it is the repository's own check, not the `doc-drift-detector` skill, so an eval needs nothing outside the repository. A failed check fails the case with a reason and skips the judge; a case whose checks pass goes on to the `llm_judge` grader with `criteria`. A `sandbox_tests` case may also carry an `expected.rubric` block, as the `rubric` grader reads it, to check the prose of an answer whose work tree is the ground truth, such as the feature-coder's summary and its map from acceptance criteria to tests (#86). The order is then the run checks, the rubric's deterministic checks against the case's work tree, and the rubric's model; the first failure stops the case. The rubric adds to the ground truth and never replaces it. The grader needs Docker and the sandbox image.

The `rubric` grader (`evals/graders/rubric.py`, #85) is for agents whose output is prose with no ground truth to compare it with, and only planning, documentation and research agents may use it as their grader; reviewers, fixers, the judge and the Jev agents keep ground-truth graders (docs/evals.md). It needs an `expected.rubric` block: `criteria` (each an `id`, a `text`, and for a `required` one a `floor`), a `threshold` on the mean of their scores, and the deterministic checks `required_fields`, `empty_fields`, `forbidden` and `paths_exist`. `empty_fields` names fields that must be present and empty, for an answer that must hold something back, such as the product-architect's `goals` on a request that contradicts itself (#90); a field cannot be both required and empty. A `paths_exist` name may hold a `*` that walks every item of a list, as in `tickets.*.impacted_paths` for story-breakdown (#91). The checks run first, after the output schema, and a failure skips the model. Haiku (`claude-haiku-5-5`, the judge model) then scores each criterion from 0 to 1 with a fixed system prompt; it is sent the criteria and the answer, never the threshold or the floors. The pass decision is code: the mean must reach `threshold` and each required criterion its `floor`. Scores that do not name exactly the given criteria are retried once, then raise `RubricError`, a case error. Each call writes one trace row, `agent_ref` `eval-rubric@claude-haiku-5-5`, under the eval's tag and pipeline run id with its cost.

A research agent's output marks facts with `Claim` and `Citation` from `registry/common_schemas.py` (#103): a claim has `text`, `fact` and `citations`, and `fact: true` requires at least one citation (`url`, `quote` copied from the page, `retrieved_at`). That is the only marking; an agent whose output holds sourced statements declares `claims: list[Claim]` (or a list nested under another field) and the cases name its path. The `citations_resolve` check (`evals/graders/citations.py`) is a deterministic step of a `rubric` case, set with `expected.citations_resolve` (`claims`, a dotted path defaulting to `claims`, and a non-empty `allow_domains`). After the rubric's own checks and before the grader model, it fetches each citation of each fact through `web.fetch`, subject to `allow_domains` as an agent run is, and requires the normalised quote in the normalised page (docs/evals.md lists exactly what is normalised). A missing quote fails the case; a URL outside the domains or a failed fetch is a case error (`CitationCheckError`), never a pass. `evaluate` and `grade` take an optional `fetcher` so tests fetch nothing. The case's `allow_domains` are also the domains its run gets: the eval runner replaces the manifest's `tools.web.allow_domains` with them for that case (`LLMSpec.with_web_domains`; replaced, not merged), a case without the check runs with the manifest list, and an agent whose manifest lists no domain gets no web tools either way. Research agents need both `rubric` and `citations_resolve` to publish (docs/evals.md).

`expected.options_in_record: true` is a deterministic step of a `rubric` case for `escalation-summary` (#94), the one `hitl` agent the rubric grader is allowed for (docs/evals.md). After the rubric's own checks and before the grader model, it runs `runtime.escalation_summary.ungrounded` on the answer against the case's payload, the cut-down record, and fails the case on any problem it returns: no option, an option whose evidence is not a span of the record, or an invented dollar figure. It is the check that runs before a summary is stored ([Summaries](#summaries)). Any other grader refuses it at load.

The `incident` grader (`evals/graders/incident.py`, #108) is for triage-diagnostics, whose cases are fixtures with a planted crash and a hand-written Sentry event. It needs `expected.verdict: null` and an `expected.incident` block: `suspected_path` (the file of the planted fault), `exception` (the class the event shows) and `crash_file` (the file of the frame that raised it, which differs from `suspected_path` when the fault is above that frame). A case passes when the answer's `suspected_paths` list `suspected_path`, and when the answer's `reproduction_test`, written into a fresh copy of the fixture at `head` (which plays the release tag), fails under pytest with `exception` raised from `crash_file`. That rule is `reproduces` in the same module, and the incident pipeline applies the same function to test-writer's tests. The grader never calls a model and needs Docker and the sandbox image.

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

- An escalation clears `skip`. A manifest that wants a particular skip to stand gives the escalating gate `unless: <skip gate>`, so that gate does not fire at all when the skip gate fires.
- A truncated input yields `skip=False, escalate=True, route=()` with reason `truncated`, whatever the answers: review everything.
- A Jev failure escalates with reason `jev_error`.
- Jev never has the last word on anything consequential.

`decide(spec, output, truncated)` in `runtime/gates.py` evaluates `JevSpec.gates` and `JevSpec.routing` on a Jev output. A gate fires on a strict comparison, a gate with `unless` only when the gate it names did not fire, and `skip` is set when a skip gate fired and no escalate gate did. `route` is the union, in manifest order, of the routing entries whose `when` is a fired gate or `question=answer` for the answer given. `reason` is the first escalating gate, else the first skipping one, else the `when` of the first matching route, else `no_gate`. The review panel runs `diff-triage@^0.4` after `show_diff` and writes the decision and the probabilities to one extra trace row (`kind` `jev`, `tag` `triage:decision`, no cost of its own). Any exception from the Jev call becomes `Decision(skip=False, escalate=True, route=(), reason="jev_error")` and the panel goes on. Whether the panel acts on the decision depends on two things: the manifest's `act_on_decision` (true for diff-triage since 0.4.0) and the caller's `act_on_triage` argument to `run_panel`, which defaults to false. Only `factory review` passes true, with or without `--comment`. The fix, build and hotfix loops review diffs the factory wrote, while the skip gate was calibrated on pull request diffs only, so they leave it off and run every reviewer. The decision row's `decision_json` holds the `Decision` fields plus `route_only` (true unless both the flag and the caller allow acting), `may_act` (what the caller allowed), `sampled` and `truncated`.

When the panel may act:

- A skip is honoured only when the status is `ok`, `decision.skip` is true, nothing escalated, the diff was not truncated and the run was not sampled. Then no reviewer, judge gate or judge runs, the change ships and `triage.outcome` is `skipped`.
- `SKIP_SAMPLE_RATE` (0.05, a code constant in `pipelines/review_panel.py`) of the runs that would skip are drawn at random for the full panel. They are marked `sampled` and run every reviewer, so `scripts/shadow_report.py` keeps measuring the skip gate. The draw is made only for a run that would skip.
- A route that names a proper subset of the selected reviewers runs only those, in panel order, and the judge gate and the judge see only them; `triage.outcome` is `routed`. A route of `all`, an empty route, a route naming an agent outside the panel's reviewers, an escalation, a `jev_error` and a truncated diff all run every selected reviewer, with outcome `full`.

The triage decision row's `cost_usd` is 0.0 because the `run_jev` row under the same `pipeline_run_id` carries the real cost of the call; charging both would count it twice in `factory cost` and `Trace.cost_by_pipeline_run`. `scripts/calibrate.py` and `scripts/shadow_report.py` rely on this and read a run's cost as the sum over its pipeline run id. Calibration runs use pipeline run ids starting with `calibrate-`, so the shadow report can leave them out ([jev-calibration.md](jev-calibration.md)).

The judge gate (`decide_judge_gate` in `runtime/gates.py`, agent `judge-gate@^0.1`) is the one place a Jev decision does skip work. After the reviewers have run, the panel renders their reviews as text and asks two questions: whether the verdict is clear-cut (noul `clear_cut`) and which of ship, request changes or unclear it is (choice `verdict`). The skip gate fires only when `clear_cut` is above 0.85 and neither escalating gate did: the `verdict` confidence below 0.8, or the answer `unclear`. A failed reviewer, a truncated rendering and a failed Jev call all send the change to the judge, with reasons `reviewer_failed`, `truncated` and `jev_error`. When the gate skips the judge, the verdict is the gate's `verdict` answer, computed in code, and `ship` is true only for `ship`. The gate decision goes to one extra trace row tagged `judge_gate:decision`, with no cost of its own, whose `decision_json` also holds the path (`skipped_judge` or `escalated`) and an estimate of what the judge would have cost. The gate call itself is charged to the panel's budget.

## Trace row

Every agent run, successful or not, writes exactly one row to the DuckDB trace store. The default path is `data/traces.duckdb`, overridden by an argument or by `FACTORY_TRACE_DB` (#6). Orchestrated workers all set `FACTORY_TRACE_DB=~/.local/share/factory/traces.duckdb`, one store outside every worktree, so `factory cost --since` covers every worker and the rows survive `git worktree remove`; a per-worktree `data/traces.duckdb` is deleted with the worktree. The package default stays `data/traces.duckdb` for a plain checkout, and the container keeps `/app/data/traces.duckdb` (#72).

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
| `parent_run_id` | VARCHAR, null | Migration 3. The `run_id` of the run this one follows: the fixer's is the test-writer's, the review panel's rows in the fix loop name the fixer, and a guard decision names the run it judged. Null for a run with no parent, which `factory trace` hangs under the pipeline root. A tree, so a stage that reads several runs (the judge, after its reviewers) names one parent at most |

A guard decision is a row of its own, written by `runtime/guard.py`. Its `decision_json` is `{"parent_run_id", "allow", "reason"}`, `parent_run_id` is the `run_id` of the guarded run, and `kind` and `agent_ref` name the guard agent. Select a run's decisions with `json_extract_string(decision_json, '$.parent_run_id') = <run_id>`.

A version becomes publishable by passing its evals. Migration 2 adds the `published` table, written only by `factory publish-check` through `Trace.record_published(agent, version, pass_rate, git_sha)` and read back with `Trace.published()`. It is an append-only history with no key: publishing a version again adds a row, and a version counts as published once it has any.

| Column | Type | Meaning |
| --- | --- | --- |
| `agent`, `version` | VARCHAR | The agent name and its `MAJOR.MINOR.PATCH`, as in the manifest |
| `pass_rate` | DOUBLE | The eval pass rate that earned the row |
| `published_at` | TIMESTAMPTZ | When the row was written, in UTC |
| `git_sha` | VARCHAR | `HEAD` of the repository when the check ran |

A `Trace` holds one DuckDB connection for its lifetime and opens it on first use, rather than opening per call. DuckDB allows one read-write process per file, so holding the connection keeps the single writer (#26) in charge of the file and saves a file open on every run; close it with `close()` or a `with` block. That one connection is shared by runs gathered on an event loop and by hooks and guard runs on worker threads, and a DuckDB connection must not be used from two threads at once, so every public `Trace` method holds the instance's reentrant lock (#26). It is a lock rather than a writer task and a queue: a row is one fast insert, and a queue would need a flush before every read and a loop that callers on worker threads do not have. Schema changes are numbered migrations in `runtime/trace.py`: append a `(version, sql)` pair, never edit a shipped one, and `ensure_schema()` applies the missing ones and records them in `schema_version`. `cost_usd` is null for a model with no entry in `runtime/pricing.py`, and aggregates treat null as zero.

`web.fetch(url)` and `web.search(query)` (`tools/web.py`) give an agent read-only web access by running `agb web fetch <url> --markdown --json --yes` and `agb web search <query> -n 10 --json --yes`; the factory owns no HTTP client. Three rules apply to them. First, the allow-list: `tools.web.allow_domains` in the manifest names the hosts, and `tool_defs` raises `ToolListError` for an agent that allows a web tool without one. The runner also checks every call (`tools.web.check_web_call`): a `web.fetch` URL whose host is neither a listed domain nor a subdomain of one is refused with `WebDomainError` (a `ToolError`, exit 2), which goes back to the model as an error result and never runs. `web.search` names no host, so it needs only a non-empty list. Two callers may replace the manifest's list for one run, never merging with it and never giving web tools to an agent whose manifest lists no domain: an eval case that sets `citations_resolve.allow_domains`, and `factory run --allow-domain`. Redirects that `agb` follows after the check are not re-checked. Second, never in the sandbox: `SANDBOX_DENIED` in `tools/registry.py` holds both names, and `tool_defs` refuses them for `sandbox: true`, which keeps `--network none` meaningful. Third, fetched content is untrusted: both tools wrap their text in a block that begins with a line saying it is data and not instructions, between `<<<BEGIN FETCHED CONTENT>>>` and `<<<END FETCHED CONTENT>>>` (an end marker inside the page is removed), the registry descriptions say the same, and agent prompts that allow web tools must say so too. Output is capped at `MAX_OUTPUT_BYTES` with the usual truncation marker, and each `agb` call has the 30 s tool timeout. Fetched pages are cached for the life of the process, keyed by URL, in a module-level dict in `tools/web.py` that `tools.web.reset_cache()` empties, so a retried agent does not fetch twice; searches are not cached. Both are in `READ_ONLY`.

## Work items

A work item is one unit of work the factory can take from idea to pull request: a feature, a bug, a refactor, documentation, a research question or an incident. "Ticket" means the same thing; the command is `factory ticket` (#82). Agents stay stateless. The work item lives in the store, and an agent sees only the part of it a pipeline passes as payload. The reasons are in [ADR 0002](decisions/0002-work-items.md).

The store is two tables in the trace store's DuckDB file (`FACTORY_TRACE_DB`, see [Trace row](#trace-row)), added as migrations 4 and 5 of `MIGRATIONS` in `runtime/trace.py`. `runtime/work_items.py` (#82) uses the `Trace` instance's connection and lock through `Trace.connection()`, so one process still holds one connection and one writer. [work-items.md](work-items.md) is the user guide. `work_items` holds one row per item:

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | VARCHAR | `wi-<n>`, `n` counting up from 1 in the store. Never reused, never a GitHub issue number |
| `kind` | VARCHAR | `feature`, `bug`, `refactor`, `docs`, `research` or `incident` |
| `title` | VARCHAR | One line, at most 120 characters |
| `body` | VARCHAR | Markdown: what to do and why |
| `acceptance` | JSON | A list of strings, at least one. Each is a criterion a test or a reader can check. A research item's criterion is the question answered |
| `repo` | VARCHAR | The repository: `owner/name` on GitHub, or an absolute path to a local checkout with no remote, such as a fixture |
| `ref` | VARCHAR | The ref work starts from. The pipeline resolves it to a commit when it starts and records that commit in the event |
| `parent` | VARCHAR, null | `wi-<n>` for another item, or `prd:<pipeline_run_id>` for the PRD a plan loop stored under `data/plans/<run-id>.md` (#92) |
| `depends_on` | JSON | A list of item ids. Each must exist, and the graph stays acyclic; both are checked on every write |
| `impacted_paths` | JSON | A list of paths relative to the repository root, empty when unknown. Written by story-breakdown from `deps.impact` (#91) |
| `size` | VARCHAR, null | `S`, `M` or `L` when story-breakdown set it |
| `status` | VARCHAR | One of the states below. A copy of the last event's `to_status`, written in the same transaction |
| `pipeline_run_ids` | JSON | Every pipeline run that worked on the item, in order, appended by `link_run` |
| `issue_number` | INTEGER, null | Migration 8 (#95). The GitHub issue `notify.issue` posts the item's escalations to, stored by `WorkItems.set_issue_number` after a real post; null until then. `factory ticket show` prints it as `issue`, and its JSON carries `issue_number` |
| `created_at`, `updated_at` | TIMESTAMPTZ | In UTC |

The cost so far is not a column. It is the sum of `cost_usd` over the trace rows whose `pipeline_run_id` is in `pipeline_run_ids`, computed on read as `Trace.cost_by_pipeline_run` does, so it can never drift from the trace.

`work_item_events` holds one row per change of status, the creation included:

| Column | Type | Meaning |
| --- | --- | --- |
| `work_item_id` | VARCHAR | The item |
| `at` | TIMESTAMPTZ | When, in UTC |
| `from_status` | VARCHAR, null | Null for the creation event |
| `to_status` | VARCHAR | The new state |
| `actor` | VARCHAR | `human` for a command a person runs, `pipeline:<module>` for a pipeline (`pipeline:build_loop`), `store` for a promotion `ready()` makes |
| `pipeline_run_id` | VARCHAR, null | The run that made the move; null for `human` and `store` |
| `reason` | VARCHAR | Why. Never empty |

Actor `store` makes only the two moves `ready()` makes, `todo` to `ready` and `todo` to `blocked`; any other move is a `human` or a `pipeline:<module>` one, and a pipeline actor must give its run id. A new item can depend only on items that already exist, so it cannot close a cycle. `WorkItems.set_depends_on` replaces the dependencies of an item in `todo` or `blocked`, refuses any other state, and is where the acyclic check runs.

An agent gets an item as the `Ticket` model in `registry/common_schemas.py`: `kind`, `title`, `body`, `acceptance`, `repo`, `ref` and `impacted_paths`. It never sees `status`, the cost, the run ids, the issue number or the events, and no tool lets an agent read or write the store. story-breakdown returns tickets in the same shape without ids, with dependencies given as indexes into its own list; the plan loop turns them into ids.

The agent is `story-breakdown@0.1` (#91). It takes a PRD, a `repository` and a `ref` and returns `summary`, `tickets` and `open_questions`; each ticket is a `PlannedTicket`, the `Ticket` fields plus `size` (`S`, `M` or `L`) and `depends_on`, a list of zero-based indexes into `tickets`. The tickets are in build order, so an index is always smaller than the ticket's own, and the output model rejects an index out of range, a repeated or self dependency and a cycle (the error names the cycle), and an `impacted_paths` entry that is absolute, has a `..` part or repeats. `impacted_paths` names existing files at the ref: the files a ticket edits plus the importers `deps.impact` returns for them; a file a ticket creates is named in its `body`. That every path exists is checked by the eval cases' `paths_exist`, since the output model does not read the repository. The parallel rule (the paragraph on parallel running under the build loop below), that two items run together only when their impacted paths are disjoint and non-empty, is in the agent's prompt, `StoryOutput.parallel_conflicts()` and a rubric criterion in each case, not a model validator: an overlap costs parallelism, not correctness, and failing the whole run on it would throw away a usable plan. The plan loop (#92, [plan-loop.md](plan-loop.md)) checks the result with `parallel_conflicts()` before it files anything and makes tickets that share a path sequential, and `PlannedTicket.as_ticket()` gives it the plain `Ticket` to file.

```json
{"id": "wi-7", "kind": "feature", "title": "Add --since to factory ticket list",
 "body": "List only items created after a date.", "acceptance": ["`--since 2026-11-01` omits older items"],
 "repo": "agb1986/agb-agent-factory", "ref": "main", "parent": "wi-3", "depends_on": ["wi-6"],
 "impacted_paths": ["factory/cli.py"], "size": "S", "status": "todo", "pipeline_run_ids": []}
```

### States

| State | Meaning |
| --- | --- |
| `todo` | Written, with dependencies not all `done` or not yet promoted |
| `ready` | Every dependency is `done`; a pipeline may take it |
| `in_progress` | One pipeline run is working on it |
| `in_review` | A pull request is open; a person decides |
| `blocked` | Stopped for a reason no human question settles: a dropped dependency, or a run that failed for a cause outside the item (sandbox, API, a killed process) |
| `escalated` | Stopped on a question for a human, held in an escalation record (#93) |
| `done` | Finished: its pull request merged, or its research answer accepted |
| `dropped` | Abandoned on purpose |

`done` and `dropped` are final. A regression in finished work is a new item with the old one as `parent`.

### Transitions

`move(id, to, reason)` checks the pair against one table in `runtime/work_items.py` and refuses every other pair with `IllegalTransitionError`, a `UsageError` (exit 2). An unknown id raises `WorkItemNotFound`, a `NotFoundError` (exit 3). The "moved by" column names the code that normally makes each move. `factory ticket move` lets a person make any legal move, with actor `human`, so work the pipelines do not cover yet, such as a research item, moves by hand along the same path.

| From | To | Moved by | When |
| --- | --- | --- | --- |
| (none) | `todo` | `factory ticket add` (#82), the plan loop (#92), the incident pipeline (#108), the hotfix pipeline (#109) | The item is written |
| `todo` | `ready` | `ready()` and `factory ticket ready` (#82), actor `store` | Every dependency is `done` |
| `todo` | `ready` | The incident pipeline (#108) | Its test reproduced the event |
| `todo` | `blocked` | `ready()`, actor `store` | A dependency was dropped |
| `todo`, `ready`, `in_review`, `blocked`, `escalated` | `dropped` | A person | The work is abandoned |
| `ready` | `in_progress` | The build loop (#87), the hotfix pipeline (#109) | The run starts, before the clone |
| `in_progress` | `in_review` | The build loop (#87) | The panel said `ship` and the pull request is open |
| `in_progress` | `blocked` | The build loop (#87), the hotfix pipeline (#109) | A stop no answer settles: a failure outside the item, a crash, or an escalation that could not be written ([Escalations](#escalations)) |
| `in_progress` | `escalated` | The pipeline that stopped, through `runtime.escalations.escalate` (#93) | A `blocked` output, a budget stop, a second `request_changes`, the item contradicting the code, or a failed check ([Escalations](#escalations)) |
| `in_progress` | `ready` | A person | A run was killed before it could move the item |
| `in_review` | `done` | A person | The pull request merged |
| `in_review` | `ready` | A person | The pull request was closed or needs changes; the reason says which |
| `blocked` | `todo`, `ready` | A person | The cause is cleared |
| `blocked` | `escalated` | `runtime.escalations.escalate` (#93) | A question for a human is written about it |
| `escalated` | `ready` | `factory escalations answer` (#93), actor `human` | The item's last open record is answered; the answers go to the next run in the ticket body |

A pipeline moves its item out of `in_progress` in a `finally` block, as the runner writes its trace row: to `escalated` through `escalate` when a person's answer can settle the stop, otherwise to `blocked`, with `<ErrorClass>: <message>` as the reason either way. Before it moves an item to `in_progress` it links its pipeline run id with `link_run`. The build loop's and the hotfix pipeline's reasons also name the report, `<ErrorClass>: <message>; report <path>`. A dry run of the build loop (`factory build --dry-run`) links its run, so the item's cost counts it, but moves no item, so the item stays `ready`.

These moves are illegal for everyone, people included:

- Any move out of `done` or `dropped`.
- Any move into `in_progress` from a state other than `ready`.
- Any move into `done` from a state other than `in_review`. Nothing skips review.
- Any move into `ready` while a dependency is not `done`.
- `escalated` to `in_progress`: an answered item goes through `ready`, so the run that takes it gets the answer.
- `in_review` to `in_progress`, and `in_progress` to `dropped`: stop the run or close the pull request first.
- A move to the state the item is already in.
- Any move an agent makes. Only pipelines and `factory` commands call `move`.

### The orchestrator

Prioritisation and assignment are code over the store, and no model decides them. This extends [Gates](#gates): no LLM assigns work, picks the next item or sets its priority. The `ready` items are ordered by this key, first difference wins:

1. `incident` before every other kind.
2. More items that depend on it, directly or through others, first. Dependents that are `dropped` are not counted.
3. The `value` of a Jev `score` question, higher first, when a ranking agent ran and its `confidence` reached the threshold in its manifest. A missing, failed or unsure answer leaves this key out; it never stops the order.
4. Older `created_at` first, then the lower id.

A Jev ranking is a trace row of its own, `kind` `jev`, tagged `rank:decision`, with the probabilities and no cost of its own, as the triage decision row is. No issue builds a ranking agent yet, and until one exists the third key is absent.

The kind decides the pipeline, by a fixed table in code. `feature` and `bug` go to the build loop with `feature-coder` (#87). `refactor` goes to the build loop with `refactorer` (#98), which also runs the behaviour check: the whole suite before and after, refusing a changed test state, a coverage drop or a changed test file ([build-loop.md](build-loop.md#the-behaviour-check-refactor)). `docs` goes to the build loop with `documentation-agent` (#97), which also runs the docs check: a round that changed code, deleted a file or created a source file is refused before the suite ([build-loop.md](build-loop.md#the-docs-check-docs)). `incident` goes to the hotfix pipeline, `factory hotfix` (`pipelines/hotfix.py`, #109, [hotfix.md](hotfix.md)), which reuses the fix loop's stage with the reproduction test as the finding and stops at a pull request labelled `hotfix` ([ADR 0004](decisions/0004-no-automated-deploy.md)). Incident tickets are filed by `factory incident` (`pipelines/incident.py`), in `ready` only when a test reproduced the event; because `ready()` also promotes an unreproduced one, which has no dependencies, the hotfix pipeline checks `is_reproduced(body)` before it acts and refuses an unreproduced one with exit 2 ([incident.md](incident.md)). `research` has no pipeline; a person runs the research agent with `factory run` and moves the item. Two items run at the same time only when code has checked that their `impacted_paths` are disjoint and both non-empty. In phase 7 there is no scheduler: a person names the item with `factory build <id>` or `factory hotfix <id>`.

### Teams

A team is a label, not a unit the registry knows. An agent's manifest may declare it with an optional `team` field, one of `planning`, `dev`, `qa`, `sre`, `research`, `decomp`, `hitl` and `analytics` (the eight teams of [ADR 0002](decisions/0002-work-items.md#the-eight-teams-of-spmd-and-the-phases)). An unknown value fails the manifest like any other bad field.

```yaml
name: feature-coder
version: 0.1.0
kind: llm
team: dev
```

The resolver, the runner and the publish gate never read `team`. It never reaches a model, and setting or changing it bumps no version, as moving a model into `registry/common_schemas.py` bumps none. `factory list` may read it to group agents. The factory metrics report (#96) does not group by team, because no manifest field exists yet; it says so in its output and gains the grouping when the field lands.

Composition lives in `pipelines/`, one module per loop, named after what the loop does: `pipelines/build_loop.py` (#87), `pipelines/plan_loop.py` (#92), `pipelines/incident.py` (#108), which runs an `sre` agent and a `qa` agent, `pipelines/hotfix.py` (#109). A loop may compose agents of several teams; the build loop runs `dev` and `qa` agents. The module's docstring names the teams it composes. There is no team object, no team manifest and no team version.

## Escalations

A pipeline that stops on something a person can settle writes an escalation record and moves its item to `escalated` (#93). The code is `runtime/escalations.py` and the table is `escalations`, migration 6 of `runtime/trace.py`. ADR 0002 sets the split: a stop a human can answer goes to `escalated`, and `blocked` keeps only failures outside the item. [work-items.md](work-items.md#escalations-and-factory-escalations) shows the commands.

### The record

A record holds `id` (`esc-<n>`), `work_item_id`, `created_at`, `actor`, `pipeline_run_id`, `trigger`, `question`, `from_status` (the item's state before the move), `pipeline_run_ids` (the item's linked runs), `cost_usd`, `diff`, `agent_outputs` (agent, run id and output text of each), `suite_tail`, and, once answered, `answer`, `answer_source` and `answered_at`. `show --json` prints these with `open`, true until the answer is stored.

- **One row per stop.** Each stop writes a new record. A record is written once and changed once, when its answer is stored. An item stopped twice has two records, and the second keeps its own question, diff and cost.
- **Cost is a snapshot.** `cost_usd` is `WorkItems.cost_usd` at the moment of the stop, not a live sum. The record shows what the work had cost when the person was asked, which is what the person weighs against the answer; a later run adds its own rows, and `factory ticket show` prints the live total.
- **Large fields are capped,** and the cut is marked in the text with a `[cut: ...]` line, so a record never grows without bound:

  | Field | Limit (characters) | Kept |
  | --- | --- | --- |
  | `question` (`QUESTION_CHARS`) | 4000 | The head |
  | `diff` (`DIFF_CHARS`) | 20000 | The head, where the first files are |
  | each agent output (`OUTPUT_CHARS`) | 8000 | The head |
  | `suite_tail` (`SUITE_TAIL_CHARS`) | 4000 | The tail, where pytest sums up the failures |
  | `answer` (`ANSWER_CHARS`) | 8000 | Nothing: a longer answer is refused, not cut, so a person's words stay whole |

- **Nothing is read from the environment.** A record holds what the caller passes and what the store already has. A caller never passes a token, a key or an environment dump; the build loop passes the workspace diff, the last coder answer and panel result, and the last suite output.
- **The record and the move are one step.** `escalate` writes the row inside `WorkItems.move_with`, so the record, the item's new status and its event commit in one transaction under the trace's single writer, and any error rolls all three back. A record never exists for an item that did not move, nor an `escalated` item without a record.
- **One entry point.** `escalate(trace, item_id, *, trigger, question, reason, actor, pipeline_run_id, diff, agent_outputs, suite_tail)` is the one function a pipeline calls; the hotfix pipeline (#109) calls it too rather than writing its own stop. It takes an item in `in_progress` or `blocked`; any other state raises `IllegalTransitionError` and writes nothing.
- **Plain functions for reuse.** `render_markdown(record)` returns the record as Markdown: the question and a summary table, then the diff, the agent outputs and the suite tail in `<details>` sections. `answer(trace, id, text, source)` stores an answer. The summary agent (#94) and the issue notifier (#95) reuse both.

### Triggers and the build loop's stop table

A record's `trigger` is one of five, `TRIGGERS` in `runtime/escalations.py`: `blocked_output`, `budget`, `changes_requested`, `contradiction` and `check_failed`. The issue names the first four; `check_failed` follows ADR 0002, because a failed check on the coder's own change is a stop a person settles by saying what the change should be.

The build loop maps each stop through `ESCALATE_ON` in `pipelines/build_loop.py`. The first row whose class the error is an instance of wins, so a subclass escalates as its parent does. Every class not listed blocks.

| Stop | Ends | Trigger | Why |
| --- | --- | --- | --- |
| `CoderBlockedError` (the coder answered `blocked`) | `escalated` | `blocked_output` | The coder asked a question; its `blocked_reason` and `open_questions` become the record's question |
| `BudgetExceeded` | `escalated` | `budget` | A person decides on a bigger budget or a smaller ticket |
| `ChangesRequestedError` (a second `request_changes`) | `escalated` | `changes_requested` | The panel and the coder disagree twice; a person says which findings matter |
| `NoChangeError` (the coder changed nothing) | `escalated` | `contradiction` | The ticket and the code disagree ([below](#how-a-contradiction-is-detected)) |
| `SuiteFailedError` | `escalated` | `check_failed` | The coder's change fails the suite; a person says what the change should be |
| `FileWriteRefusedError` | `escalated` | `check_failed` | The coder wrote outside its policy; a person narrows the ticket or widens the policy |
| `TestFileChangedError`, `BehaviourChangedError`, `CoverageDroppedError`, `CoverageUnmeasuredError` | `escalated` | `check_failed` | A refactor changed what the code does; a person says whether the ticket is a refactor |
| `CodeChangedError`, `UnparsableSourceError`, `SourceFileActionError`, `UncheckedFileError` (the docs check, #97) | `escalated` | `check_failed` | A docs round changed code, left a file unparsable, created or deleted a file, or edited a file the check cannot read; a person says whether the ticket needs a code change or narrows it to docs |
| `BaselineNotGreenError`, a bare `BehaviourError` | `blocked` | | The suite at the ticket's ref fails before the coder runs: the repository, not the ticket, needs fixing |
| `BaseUnparsableError`, a bare `DocsCheckError` | `blocked` | | A Python file the round changed already did not parse at the ticket's ref, so no answer about the round fixes it, the same reasoning as `BaselineNotGreenError`; a bare `DocsCheckError` is the check run out of order, a bug in the loop |
| A bare `BuildError` (such as a failed fetch) | `blocked` | | A git or workspace failure outside the item |
| `ToolError`, `ToolExecutionError` (the sandbox) | `blocked` | | Docker or the sandbox failed |
| `JevError` | `blocked` | | The Jev service failed; nothing about the item changed. Its docstring's `jev_error` reason is not a trigger |
| `AgentError`, `UsageError` and every other `FactoryError` | `blocked` | | An API, manifest or outside-write failure outside the item |
| Any other exception (`RuntimeError`, a killed process) | `blocked`, or left `in_progress` if the process died | | A crash; the error propagates after the move |
| An escalation that cannot be written | `blocked` | | The stop is kept as `blocked` and the report's `board_error` says why |

A dry run writes no record and moves no item. The build report's `escalation` is the record's id, or null.

The hotfix pipeline (#109) maps its stops through its own `ESCALATE_ON` in `pipelines/hotfix.py`, with the same first-match rule (`stop_trigger(exc, table)`). Its fixer has no `blocked` answer, so it has no `blocked_output` row.

| Hotfix stop | Ends | Trigger | Why |
| --- | --- | --- | --- |
| `HotfixChangesRequestedError` (the one panel round asks for changes) | `escalated` | `changes_requested` | A person says which findings matter, or fixes it by hand |
| `BudgetExceeded` | `escalated` | `budget` | A person decides on a bigger budget or a smaller fix |
| `TestFileWriteError` (the fixer wrote to the protected reproduction test) | `escalated` | `check_failed` | The fixer tried to change the test, not the code; a person says what the fix should be |
| `SuiteFailedError` (the fix loop's) | `escalated` | `check_failed` | The fix fails the suite; a person says what the change should be |
| `FixError` (the fixer changed nothing) | `escalated` | `contradiction` | The ticket says the event needs a fix and the fixer found nothing to change |
| `HotfixError` (the test no longer reproduces at the ticket's ref) | `escalated` | `contradiction` | The ticket and the code disagree: the incident may be fixed already, or the ticket needs `factory incident` again |
| `OutsideWriteRefused` (a missing `hotfix` label, an existing branch) | `blocked` | | An operator precondition on the repository, outside the item |
| A bare `BuildError`, `ToolError`, `JevError`, `AgentError`, `UsageError` and every other `FactoryError` | `blocked` | | A failure outside the item, as in the build loop |
| Any other exception | `blocked` | | A crash; the error propagates after the move |
| An escalation that cannot be written | `blocked` | | The report's `board_error` says why |

The hotfix report's `escalation` is the record's id, or null. Its record carries the fixer's commit diff, the fixer's and the panel's outputs and the last suite tail. No `ticket_for_run` step feeds the answers to the next hotfix run yet; a person reads them and moves the item.

### How a contradiction is detected

No model judges a contradiction; the loop uses a fact it can check. The coder answered without `blocked`, yet its commit changed no file (`NoChangeError`, a `BuildError`). The ticket asks for a change, so either the code at the ticket's ref already does what it asks, or the ticket is wrong about the code, and only a person can say which. A coder that sees the contradiction itself should answer `blocked` with an open question, which escalates as `blocked_output`.

### Posting to GitHub

`notify.issue` (#95) posts an escalation to the ticket's GitHub issue, so a person sees it where they already work. It is an outside write under [Outside writes](#outside-writes), listed by `escalation-summary`, and `runtime/notify.py` is the one caller: `notify_escalation(trace, escalation_id, *, agent, context, summary)` renders the record, posts it through `runtime.outside.write`, stores the issue number on the item and returns one entry for the pipeline's report, `{"outcome", "action", "url", "issue", "error"}`, with `body` added in a dry run. It never raises, so a refused or failed post cannot change the item's end state or the pipeline's exit code; the entry's `outcome` and `error` say what happened.

- **When it runs.** The post is the second step of `runtime.after_escalation.after_escalation` ([Summaries](#summaries)), right after the summary step, so every stop path that writes a record posts it: the build loop and the hotfix pipeline's `_stop_item`. It is made on behalf of the resolved `escalation-summary` agent, which lists `notify.issue` in `tools.allow`, with the pipeline's `OutsideContext`; when the summary step stored a summary, the post's trace row has the summary's run as its `parent_run_id`, and otherwise none. It runs after a budget stop too, since a post costs no tokens.
- **What it posts.** The step reads the stored summary with `runtime.escalations.get_summary` and passes it as `summary`, so the issue shows the summary above the record when there is one, and the record alone when the summary was skipped, failed or was refused.
- **When it is skipped.** In a dry run (no record is written, so there is nothing to post), and when no version of the escalation-summary agent resolves, as for the summary step. The reason is in the report.
- **What the report says.** `after_escalation.notify` is `{"done", "post", "reason"}`: `done` is true only for a written post, `post` is the entry above (null when the step was skipped), and `reason` says why it was skipped, refused or failed. A post that fails or is refused leaves the item `escalated`, the record open and the exit code the stop's; a person can still answer with `--text`.

- **One issue per ticket.** The issue is the one the work item's `issue_number` names, else the one whose body carries the ticket's marker prefix `<!-- factory:notify.issue ticket-<id> ` and was opened by the login `GH_TOKEN` belongs to, else a new one. The first escalation of a ticket opens the issue, its post being the issue body; a later escalation comments on it; a rerun for the same escalation finds its own post by the escalation's marker and edits it, the issue body or the comment. A stored number that names a pull request, or an issue the token's login did not open with the ticket's marker, is refused.
- **A closed issue.** A person may close the issue. The next escalation of that ticket still comments on it, and a rerun still edits, and the issue stays closed: the tool never reopens it. The result's target says `state: closed`, so the report shows it.
- **Only title and body.** Opening sends `title` and `body`; a comment or an edit sends `body` alone. Nothing closes, reopens, labels, assigns or locks an issue. The title is `<id>: <item title>`, neutralized like the body and cut to 256 characters.
- **The repository.** The post goes to the pipeline's target, the `origin` of the repository it was given. A work item whose `repo` is `owner/name` must name that same repository or the post is refused; an item whose `repo` is a local path is posted to its `origin`.
- **The body.** `escalation_body(record, summary)` puts the summary first under `## Summary`, then a line saying how to answer, then `render_markdown(record)`. Nothing from the environment goes in: the body holds the record and the summary, and the footer the run id and the marker.
- **Fitting 65536 characters.** GitHub refuses a body over 65536 characters. The body leaves 512 of them for the footer and fits the rest by cutting the `<details>` sections, never the question: the diff and each agent output are cut from their end and the suite tail from its start, to half, a quarter and a tenth of their size in turn; when that is not enough, all three are left out and a line says `factory escalations show <id>` prints them. A summary that still does not fit is cut from its end, and only a body that fits none of these is cut from its end as a whole. Every cut is marked with the record's `[cut: ...]` line.
- **No breakout.** A code fence the question or the summary opens and never closes is closed before the next section, so the rest of the post is not swallowed into it; the diff, outputs and suite tail are already fenced longer than any backtick run they hold. `<!--` and `-->` in posted text get a zero-width space inside them, so the text can neither open an HTML comment nor close the marker, and a marker search never matches text the record carried.
- **No mentions or references.** `neutralize` puts a zero-width space (U+200B) after an `@` that starts a mention, after a `#` before a digit (`#12`, `owner/name#12`), between `GH` and `-12`, and inside `github.com` in an issue, pull request or discussion URL. The post then pings no one and leaves no cross-reference on another issue, and it reads the same. The stored record is unchanged, so `factory escalations show` prints the text as it was.
- **The once rule.** A pipeline run posts once per ticket. A rerun is a new pipeline run, so it may post again, and it edits.

### Answers

`answer(trace, id, text, source)` stores a person's answer. `source` is `cli` for `--text TEXT` and `file:<path>` for `--text @path`.

- When the answer closes the item's last open record, the item moves from `escalated` to `ready` as actor `human`, in the same transaction as the answer.
- With several open records on one item, each answer is stored, and only the answer that closes the last one moves the item.
- A second answer to the same record is refused (exit 2): an answer is never replaced.
- An answer to a record whose item is no longer `escalated`, because a person moved it by hand, is refused (exit 2) and nothing is stored: the question no longer stands. The record stays open, so `list --open` still shows it.
- An empty answer, one over `ANSWER_CHARS`, or an item whose dependency is no longer `done` is refused (exit 2), and nothing is stored. An unknown id exits 3.

`--from-issue` takes the answer from the ticket's issue instead of the command line, through the same `answer` and the same refusals as `--text` (`runtime.notify.answer_from_issue`). It only reads GitHub, on the repository the item names: `owner/name`, or the `origin` of a local path.

- The answer is the latest comment on the issue the item stores that was created after the escalation's post, carries no factory marker (a factory post is never an answer, whoever's token wrote it), is not empty, is not by a bot, and whose author's `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`.
- This trusts everyone the repository lets comment with one of those associations: any of them can answer, and the answer goes into the next run's ticket. A comment by anyone else, such as a passer-by on a public repository, is ignored. The owner accepted this on 2026-11-21 for a repository with one person on it; look at it again before the repository gains a member or collaborator, since each of them could then answer an escalation.
- The source is `github:<owner/name>#<N> comment <id>`, naming the issue and the comment; `--json` adds `"comment": {"id", "author", "url"}`.
- It refuses (exit 2), storing nothing, when the record is already answered, the item has no stored issue, the escalation was never posted on the issue, no comment qualifies, or the latest one is over `ANSWER_CHARS` (it is not cut, as for `--text`). `--text` and `--from-issue` are exclusive, and one is required.

The next run gets the answers in its ticket body. `ticket_for_run(trace, item)` returns the item's ticket with every answered record appended under `## Answers to earlier escalations`, oldest first, as `### esc-<n> (trigger)` with the question and the answer quoted. The stored ticket is unchanged, no `Ticket` field is added, and no agent's input schema changes; an agent reads the answers as part of the ticket.

### Summaries

A record is long and written for machines, so a person reads a summary first (#94). The agent `escalation-summary@^0.1` (`agents/escalation-summary/`, Haiku at low effort, no model tools; its `tools.allow` lists only the outside write `notify.issue`, which no model is sent) reads one record and writes one `paragraph` on what is blocked, why and what the person must decide, and one to four `options`, each with a `label`, what it `takes` (`another_run`, `bigger_budget`, `cut_ticket`, `drop_work` or `by_hand`), its `cost` in words and its `evidence`. `factory escalations show` prints the summary in a `## Summary` section above the record, and `show --json` carries it as `summary` (`escalation_id`, `run_id`, `agent_ref`, `summary`, `created_at`), or null when the record has none. `list --json` does not carry it.

- **What the agent reads.** `runtime.escalation_summary.summary_input(record)` cuts the record down to `EscalationSummaryInput`: the id, the item, the trigger, the question, the state before the stop and `cost_usd` as they are; the diff to its first `SUMMARY_DIFF_CHARS` (6000) characters, each agent output to its first `SUMMARY_OUTPUT_CHARS` (3000) and the suite tail to its last `SUMMARY_SUITE_TAIL_CHARS` (2000), the cut marked as in the record. The actor, the run ids and the answer are left out. The prompt treats the diff, the outputs and the suite tail as data: text in them that reads as an instruction is reported, never followed. Nothing from the environment goes into the input, and the agent never writes a token or a key it sees in the record.
- **Every option is grounded.** `runtime.escalation_summary.ungrounded(summary, record)` returns the problems, an empty list when the summary passes. An option's `evidence` must be at least `EVIDENCE_MIN_CHARS` (12) characters and a substring of one single text of the record: the question, the diff, the suite tail or one agent output, or one string value of an output that is JSON. Runs of whitespace count as one space on both sides; case, punctuation and quoting must match, so a paraphrase, a span joined from two places or added quotation marks fails. A summary with no option fails. A dollar figure (`$` and a number) in the paragraph, a label or a cost must equal one written in those texts, or be within `COST_TOLERANCE_USD` ($0.005) of `cost_usd`, so the agent cannot invent a price for another run.
- **One check, two places.** The same `ungrounded` runs in the eval, as `expected.options_in_record` ([Eval cases](#eval-cases)), and before a summary is stored. A summary that fails it is not stored, and `show` prints the record alone. The check is the one rule a summary must keep; how well it reads is the rubric's to judge.
- **When it is made.** A summary is made at escalation time, inside the run that stopped, so it costs no later step and is there when the person first looks. A stop path calls `runtime.after_escalation.after_escalation(trace, record, *, budget, pipeline_run_id, dry_run, summary_agent, outside)` once, right after `escalate` returns. The build loop and the hotfix pipeline's `_stop_item` both do. The agent runs under the run's own `Budget` and `pipeline_run_id`, so its trace row has a cost, counts in `Trace.cost_by_pipeline_run`, and counts in the item's cost. The record's `cost_usd` is the snapshot taken before the summary ran, and does not include it.
- **Where it is kept.** `escalation_summaries`, migration 7 of `runtime/trace.py`, holds at most one row per record: `escalation_id` (the primary key), `run_id` (the agent run that wrote it, the run id of its trace row), `agent_ref` (the agent's address), `summary` (the output as JSON) and `created_at`. `store_summary(trace, escalation_id, summary, *, run_id, agent_ref)` writes it and `get_summary(trace, escalation_id)` reads it. A second summary of the same record, an empty run id or agent, and an unknown record are refused. The record itself is never changed.
- **When there is none.** The step is skipped, with its reason in the report, in a dry run (nothing is written), for a `budget` stop (the money is gone, and spending more of it on prose would break the budget the person set), when the budget has nothing left, and when no version of the agent resolves. A pipeline resolves the agent with `resolve_summary_agent(allow_unpublished=..., trace_path=...)` next to its own agents; until a version is published, a run without `--allow-unpublished` has no summaries and runs as before.
- **It never changes how the run ends.** An agent error, a summary that fails the check and a store that fails all leave the record without a summary. `after_escalation` never raises: the item stays `escalated`, the report's `escalation` is the record's id, and the exit code is the stop's. The report's `after_escalation` says what happened, `{"summary": {"done", "run_id", "reason"}, "notify": {"done", "post", "reason"}}`, with the run id whenever the agent ran; it is null when no record was written.
- **The next step.** The second step is the issue post (#95, [Posting to GitHub](#posting-to-github)). It takes the pipeline's `outside: OutsideContext`, so a stop path that calls `after_escalation` gets the post without a change of its own. `escalation-summary` 0.1.0 lists `notify.issue` in `tools.allow`; `tool_defs` drops outside writes, so the model still has no tools. A later step goes after the post, under the same rule: it never raises and never changes how the run ends.

## Secrets

`ANTHROPIC_API_KEY` and the Jev key are read from the environment and from nowhere else. The Jev key is `TYPESAFE_API_KEY`, confirmed by #15 in [jev-sdk.md](jev-sdk.md).

- Store keys with `agb sys secrets set <NAME>`, which writes them to a mode-600 `secrets.env`.
- The factory never reads that file. Whoever starts the factory exports the key: the shell, CI, or the `agb factory` wrapper (#30). In a container the same variables come from an env file, `.env` copied from `.env.example`, passed with `--env-file` or Compose's `env_file` ([Deployment](#deployment)).
- A missing key raises `AuthOrConfigError` (exit 4) with a hint that names the variable, never its value. So does a key the API rejects with a 401 or 403, with the same hint.
- No secret goes into the repository, a test, a config file, a container image, a trace row, a log line or an error message. The `typesafe_sdk` logger logs each request id at INFO, so a deployed factory runs it at WARNING (`TYPESAFE_LOG_LEVEL=warning`, which the SDK reads at import) and never at DEBUG.
- Tests that need a live key carry a marker, and `tests/conftest.py` deselects each marker unless `FACTORY_LIVE=1` is set, then skips each marker when its variable is unset: `live` needs `ANTHROPIC_API_KEY` and `live_jev` needs `TYPESAFE_API_KEY`. The markers are independent, so having one key does not run another marker's tests (`live_sentry` needs `SENTRY_AUTH_TOKEN`, `live_github` needs `GH_TOKEN`).
- The Sentry tools (`sentry.list_events`, `sentry.read_issue`) read `SENTRY_AUTH_TOKEN` (required, an auth token with `project:read` and `event:read`), `SENTRY_ORG` and `SENTRY_PROJECT` (the default project; the tool's `project` argument overrides it) from the environment, and `SENTRY_BASE_URL` (default `https://sentry.io`) for a self-hosted server. A missing token, organisation or project, or a 401 or 403 from Sentry, is a `ToolConfigError` (exit 4) that names the variable and never its value. The tools need the network, so `tool_defs` refuses them for an agent with `sandbox: true` (`SANDBOX_DENIED` in `tools/registry.py`). Their tests run on recorded responses in `tests/tools/fixtures/sentry/` through `httpx.MockTransport`; the one live test carries the `live_sentry` marker and needs `FACTORY_LIVE=1` and `SENTRY_AUTH_TOKEN`.
- The GitHub tools (`github.read_issue`, `github.read_pr`, `github.create_pr`, `github.comment_pr`) call `gh api --hostname github.com` and read `GH_TOKEN` from the environment, a token that can read the repository and, for the two outside writes, write its pull requests and contents. A missing token or a 401 is a `ToolConfigError` (exit 4) that names the variable and never its value, and the token never appears in a result, an error or a trace row. The tools need the network, so they are in `SANDBOX_DENIED`. Their tests run against a fake `gh` on `PATH` (`tests/gh_fake.py`) and push to a bare repository under `tmp_path`; the one live test reads an issue and a pull request of this repository, writes nothing, carries the `live_github` marker and needs `FACTORY_LIVE=1` and `GH_TOKEN`.

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

Every change adds a fragment, `changelog.d/<id>.<section>.md`, in the same pull request, never a line in `CHANGELOG.md`: two pull requests that both append to the same list conflict, two that each add a file do not. `id` is the issue the change closes (or a short slug when there is none), `section` is one of `added`, `changed`, `deprecated`, `removed`, `fixed` or `security`, and the content is the bullet line, or lines, as they go into the changelog, in Keep a Changelog 1.1.0 form and written for a user, not as a commit title. `scripts/collect_changelog.py` moves the fragments under `## [Unreleased]` at release time and `--dry-run` shows what a release would gain; the format is in [changelog.d/README.md](../changelog.d/README.md). The package version lives only in `version` in `pyproject.toml`; code reads it with `importlib.metadata.version('agb-agent-factory')` and never carries a `__version__` literal. The release steps, and what counts as a patch, minor or major release, are in [releasing.md](releasing.md).

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

The document also holds `min_recall_risky`, `recall_risky`, `skip_rate` and `escalation_rate` after `pass_rate`, and each case holds `decision` after `grader_fallback`; they are null for an agent whose cases set no `decision` (see "Eval cases"). The exit code is 1 also when `recall_risky` is below `evals.min_recall_risky`.

`factory publish-check <name>[@range] [--json] [--workers N]` runs the same evaluation as `factory eval` and, only when it passes, writes exactly one row to the `published` table (see "Trace row"). It writes nothing on a fail. Output and exit codes match `factory eval`: the table on stderr, and under `--json` one object holding the eval keys plus `published` (a boolean) and `git_sha`, printed also on a fail. It exits 0 when the version was published and 1 when the pass rate is below the floor, 3 for an unknown agent or version, and 4 when git cannot give the commit to record. Commit the change first: the recorded `git_sha` is `HEAD`, so it names the commit that carries the prompt only if the working tree is clean.

`factory review <repo_path> (--base <ref> --head <ref> | --pr N [--comment [--dry-run]]) [--json] [--reviewers a,b] [--allow-unpublished] [--no-triage] [--no-judge-gate] [--budget-usd N]` runs the review panel in `pipelines/review_panel.py`. It resolves `code-reviewer@^1.2`, `security-reviewer@^1.0`, `perf-reviewer@^0.3`, `judge@^2.0` and, unless `--no-triage`, `diff-triage@^0.4` up front, published versions only unless `--allow-unpublished`, and exits 3 before any request if one is missing. `--reviewers` runs a subset of the three, in panel order; an unknown name is a usage error. Triage acts inside that subset, so a route can narrow it further; `--reviewers` with `--no-triage` runs exactly the named reviewers. The reviewers run concurrently, gathered with `asyncio.gather` on `arun` on one event loop that the command starts, and every run, the judge's included, carries the same `pipeline_run_id`, so `Trace.cost_by_pipeline_run` gives the cost of the whole panel. A reviewer that raises is reported with its error and the panel goes on; the judge sees it under `failed_reviewers`. If all of them fail, nothing is judged and the command exits 1. The judge returns the four scores and `runtime.scoring.assess` turns them into `weighted_score` and `ship`. Under `--json`, stdout carries one object with `pipeline_run_id`, `base`, `head`, `reviewers` (each with `agent`, `version`, `ref`, `status`, `verdict`, `finding_count` and `error`), `judge` (`ref` and `output`), `weighted_score`, `ship`, `total_cost_usd`, `duration_ms`, `triage` and `judge_gate`, and the text report goes to stderr. It exits 0 when `ship` is true, 1 when it is false or on a hard error, 2 for usage, 3 for an unknown agent, repository or ref, and 4 when `ANTHROPIC_API_KEY` is not set. `triage` holds `status` (`ok`, `jev_error` or `disabled` for `--no-triage`), `ref`, `decision` (the `Decision` fields), `probabilities`, `truncated`, `route_only`, `may_act` (true here, false in the fix, build and hotfix loops), `sampled`, `outcome` (`skipped`, `routed` or `full`) and `error`; with `--no-triage` the Jev call is not made and every selected reviewer runs. The panel acts on the decision as described in "Gates" above. When triage skips the review, `reviewers` is empty, `judge` and `weighted_score` are null, `ship` is true, `judge_gate.status` is `triage_skipped`, the command exits 0, and the text report says that triage skipped the review, names the gate and the probability it read, says that no reviewer or judge ran and that `--no-triage` forces a review. A skip never reads as reviewers that ran and found nothing. A failed Jev call (a missing `TYPESAFE_API_KEY` included) runs every reviewer and does not change the exit code. `judge_gate` holds `status` (`ok`, `jev_error`, `not_run`, `disabled` for `--no-judge-gate` or `triage_skipped`), `ref`, `decision`, `probabilities`, `truncated`, `path`, `verdict`, `judge_cost_estimate_usd` and `error`. When the gate skips the judge, `judge` and `weighted_score` are null, `ship` follows the gate's verdict, and `judge_gate.path` is `skipped_judge`; otherwise the judge runs as before and `path` is `escalated`. `--no-judge-gate` always runs the judge. The cost of triage and of the gate is in `total_cost_usd` and counts against the budget. Every run of the panel, triage and the judge included, is charged to one `Budget` of `--budget-usd` dollars (default `DEFAULT_BUDGET_USD`, 5.0); the first run to pass it ends the panel with `BudgetExceeded`, exit 1, and the message carries the spend so far. `tests/pipelines/golden/review-panel.json` is a normalised example.

`--pr N` reads pull request N of the repository's `origin` with `github.read_pr` (it needs `GH_TOKEN`) and reviews the diff from the merge base of its base and head to its head, so `--pr` replaces `--base` and `--head` and giving both is a usage error, as is giving neither. Both commits must be in the local clone; a missing one exits 3 before any model is called, with `git fetch origin pull/N/head` as the hint. `--comment` posts the result back through the outside write `github.comment_pr` of [ADR 0003](decisions/0003-outside-writes.md), made by pipeline code for the judge's manifest, never by a model. The judge's merged findings become inline comments, one per file and first line (findings that start on the same line share a comment, worst severity first), placed on the head revision; findings on lines outside the diff are listed in the review's body by `github.comment_pr`. The body is the verdict, the weighted score, the judge's rationale and the four scores. When the judge gate skipped the judge there are no findings to place, so one summary comment gives the gate's verdict and what each reviewer found. When triage skipped the review, the summary comment is headed "Review: skipped by triage", names the gate that skipped, says that no reviewer or judge looked at the change and that this is not a review that found nothing, and gives the `factory review <repo_path> --pr N --comment --no-triage` command that forces a full review. A rerun on the same pull request edits the factory's earlier review, which `github.comment_pr` finds by its marker, instead of adding another. `--comment` needs `GH_TOKEN`, and a missing one exits 4 before any model is called. `--dry-run` (which needs `--comment`) runs the panel and every GitHub read but sends nothing; the `comment` section says what would have been done. `--comment` and `--dry-run` are both usage errors without `--pr`.

The document gains a `comment` key: null without `--comment`, otherwise `outcome` (`written`, `dry_run`, `refused` or `failed`), and for a post that happened or was planned `action` (`create` or `edit`), `url` (null in a dry run) and `body`, or for one that did not `error` and `hint`. The call writes one trace row tagged `outside:github.comment_pr` under the panel's `pipeline_run_id`, with the judge's row as its parent when the judge ran. A post that is refused or fails does not hide the review: the document and the text report are still printed, the error and its hint follow on stderr, and the command exits with the error's code (2 for a refusal such as a head that moved since the review, 1 for a failed call) instead of the `ship` code. The write is made once per run: it is attempted after the panel has finished, so its cost is nil and no retry is made inside the run.

`factory triage <repo_path> --base <ref> --head <ref> [--json] [--allow-unpublished]` runs only the triage of the panel: `diff-triage@^0.4` on the diff between the refs, then `decide`. Under `--json`, stdout carries one object with `pipeline_run_id`, `base`, `head` and the keys of the `triage` entry above, and the text report goes to stderr. It exits 0 when the Jev call succeeded and 1 when it failed (status `jev_error`, with the escalation printed), 2 for usage, and 3 for an unknown agent, repository or ref or an unpublished `diff-triage`. It needs `TYPESAFE_API_KEY` and not `ANTHROPIC_API_KEY`.

`factory fix <repo_path> --finding <json|@file> [--finding-index N] [--head <ref>] [--out-dir DIR] [--work-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]` runs the fix loop of `pipelines/fix_loop.py` on one finding: `test-writer@^0.2` writes tests for it, `fixer@^0.1` changes the code behind its action guard, the whole suite runs in the sandbox, and the panel of `factory review` reviews the change, all under one `pipeline_run_id` and one budget. `--finding` is a `Finding`'s JSON or a `factory review --json` document, whose judge's merged finding number `--finding-index` (default 0) is taken. The source repository must have nothing uncommitted and is never changed; the agents work in a clone under `--work-dir` (default: the system temporary directory), deleted at the end. Once the panel has run, the report goes to `<out-dir>/<pipeline_run_id>.json` (default `data/fixes`), and the patch to `<pipeline_run_id>.patch` beside it only when the panel approves. Under `--json`, stdout carries the report: `pipeline_run_id`, `repo_path`, `head`, `finding`, `test_writer` and `fixer` (`ref` and `output`), `files_changed` (each `path`, `action` and `by`), `suite` (`argv`, `exit_code`, `passed`, `seconds` and `output_tail`), `panel` (the `factory review` document), `ship`, `patch_path`, `report_path`, `total_cost_usd` and `duration_ms`. It exits 0 when the panel approves, 1 when it does not or when the loop fails (`TestFileWriteError`, `SuiteFailedError`, `FixError` when nothing changed, `BudgetExceeded`, or a `ToolExecutionError` from git on the clone, such as a clone over the 30 s limit or one that cannot be cleaned), 2 for usage or uncommitted changes, 3 for an unknown agent, repository or ref, and 4 for a missing `ANTHROPIC_API_KEY`, Docker or sandbox image. [fix-loop.md](fix-loop.md) describes the flow.

`scripts/check_version_bump.py [base]` is the guard behind the rule in "Agent address". It compares the working tree with the merge base of `base` (default `origin/main`) and exits 1, naming each agent, when its `system.md`, its `schemas.py` or the `questions` block of its `agent.yaml` changed while `version` did not. An agent directory that is new since the merge base needs no bump. It exits 2 when it cannot run, for example for an unknown base. It runs as the `version-bump` pre-commit hook, and CI runs it as `uv run python scripts/check_version_bump.py origin/main`. Edits to `registry/common_schemas.py` are not covered, because that module belongs to no single agent. Nor are the `team` and `files` blocks: neither reaches the model, so neither needs a bump.

`factory cost --since <date> [--by agent|pipeline] [--json]` prints what the runs in the trace store cost from `--since` on. `--since` is a date or an ISO 8601 datetime, taken as UTC when it has no offset, and is required; a value that does not parse is a usage error. Runs are grouped by agent (`--by agent`, the default) or by pipeline run (`--by pipeline`), and split into `other`, `eval` and `publish` buckets by the row's tag. Under `--json`, stdout carries `since`, `by`, `total_runs`, `total_cost_usd` and `buckets`; each of the three buckets has `runs`, `cost_usd` and `groups`, a list of `{agent or pipeline_run_id, runs, cost_usd}` in which a run outside any pipeline is `(none)`. The text output has one table per non-empty bucket. `tests/runtime/golden/` holds the text and by-pipeline JSON examples, and `tests/golden/cost.json` pins the `--json` document with the other commands.

`factory cost --by kind` groups the same runs by runner kind, `claude` for an LLM run and `jev` for a Jev run, under the key `kind`; `tests/golden/cost-by-kind.json` and `tests/runtime/golden/cost-by-kind.json` pin it. `scripts/triage_saving.py` measures what diff-triage would save: it runs the review panel over every fixture twice, with triage and as `--no-triage` would, with the judge gate off in both, and reads the cost of each run back from the trace by pipeline run id. Its pipeline run ids start with `triage-saving-`, then a token per invocation, the fixture name and `triage` or `no-triage`. The projected saving is the reviewers' and the judge's cost in the with-triage runs of the fixtures the skip gate would skip, less the triage agent's cost over every fixture. The script exits 1 when a would-skip fixture has a `major` finding or worse, a run fails, or `--max-usd` stops it, and like `scripts/shadow_report.py` it never prints a diff, a fixture's file name or a finding's text ([jev-calibration.md](jev-calibration.md)).

`scripts/factory_report.py [--since DAY] [--until DAY] [--judge-manifest PATH] [--trace PATH] [--json]` is the factory metrics report (#96), one run with no scheduler. The window is the UTC days `--since` to `--until`, both included, defaulting to the seven days ending today. It reports tickets per state at the end of the window, the time tickets spent in each state, moves into `escalated` and `blocked` and the tickets with an event, the cost of the tickets' runs in total, per ticket and per lifecycle phase, the review panel's ship rate, the rubric grader's scores per day and the cost per agent version, as Markdown or, with `--json`, as one document. The queries live in `runtime/reporting.py`, so `agb factory` (#30) reuses them instead of re-deriving them. Each takes a read-only connection from `open_store(path)` and a window of two UTC-aware datetimes, writes nothing and adds no migration; a naive or inverted window is a `ValueError`. The conventions are:

- Time in each state and the escalation counts read `work_item_events`; a ticket's state at a time is its last event before it. The creation event has no `from_status`. `done` and `dropped` are not timed.
- Cost is a read over the trace: a run is a ticket's when its `pipeline_run_id` is linked to the ticket, and a run linked to two tickets counts once for each. A run's phase is the ticket's state when the row started, and `(unattributed)` when the row started before the ticket's first event.
- The ship rate counts the judge's rows by `runtime/scoring.py` with the `scoring` block of the judge manifest, and the runs the judge gate skipped as shipped when the gate's verdict is `ship`. A judge output without the four scores is counted apart and left out of the rate. Rows tagged `outside:`, which record an outside write under the agent it is made for (ADR 0003), are not judge runs and are left out of the ship rate and of the runs per agent version; they cost nothing.
- Escalations are moves into `escalated`, as the work-item events record them, with moves into `blocked` counted beside them. #93 may refine this when it records why an item escalated.
- Agents are not grouped by team, since the manifest has no `team` field. The report prints a line saying so.
- A window with no trace row and no work-item event prints "No activity in this window" instead of a table of zeros, its JSON has `"empty": true`, and the exit code is 1. Otherwise the exit codes are `factory`'s: 2 bad arguments, 3 no trace store, 4 a bad manifest. Like `scripts/shadow_report.py`, it prints counts, scores, costs and ticket ids, never a prompt or a diff.

`factory run <name>[@range] --input <path|-> [--repo PATH] [--allow-domain HOST ...] [--json] [--allow-unpublished] [--budget-usd N]` runs one `kind: llm` agent once on a JSON payload and prints its output (`runtime/run_once.py`). It is how a person or a script uses an agent that no pipeline calls yet, such as a research agent. Without a range it takes the highest version, and like the pipelines it takes published versions only unless `--allow-unpublished`. `--input` is a file holding one JSON object, or `-` for stdin; when the agent's input model has a `repo_path` field it is set to `--repo` (default: the current directory), as the eval runner does. Each `--allow-domain` names a bare lowercase host, and together they replace the manifest's `tools.web.allow_domains` for this run; the manifest must still list at least one, so passing it to an agent without web tools is a usage error. The run is charged to one `Budget` of `--budget-usd` dollars (default `DEFAULT_BUDGET_USD`) and traced with the tag `run:<name@version>`; its run id is also its pipeline run id, so the cost includes an action guard's runs. Under `--json`, stdout carries one object with `agent`, `version`, `run_id`, `cost_usd`, `duration_ms` and `output`; without it the output is pretty-printed JSON on stdout and one line with the address, run id, cost and duration goes to stderr. It exits 0 on success, 1 when the run fails (a budget, token or turn limit, or output that never validates), 2 for usage (a bad reference, payload, repository or host, or a Jev agent), 3 for an unknown or unpublished agent, and 4 when `ANTHROPIC_API_KEY` is not set. `tests/golden/run.json` pins the `--json` document.

```console
$ factory run product-architect --input idea.json --repo . --json
{"agent": "product-architect", "version": "0.1.0", "run_id": "…", "cost_usd": 0.21, "duration_ms": 48000, "output": {…}}
```

`factory deps <repo> [--impact PATH ...] [--json]` prints the import graph of the work tree at `<repo>`, the text that `deps.graph` returns, or with `--impact` the modules that import the given files and their depth, as `deps.impact` returns. A path that is not a module of the graph exits 3, one outside the root exits 2. Under `--json`, stdout carries `modules` (`{path, language}`), `edges` (`{source, target, line}`), `unresolved` (`{source, line, specifier, reason}`), `external` (`{source, line, specifier}`), `skipped` (`{path, reason}`) and `impact`, which is null without `--impact` and otherwise `{paths, impacted: [{path, depth}]}`. `tests/golden/deps.json` pins it.

`factory ticket add|list|show|move|link|ready` reads and writes the work items in the trace store ([Work items](#work-items), [work-items.md](work-items.md)). Each takes `--json`. `add`, `show`, `move` and `link` print one item with `cost_usd` and its `events`; `list [--status S]` prints `{"work_items": [...]}`; `ready` prints `{"promoted", "blocked", "ready"}`, `ready` in the orchestrator's order. A person's `move` is logged with actor `human` and needs `--reason`. A bad field, an empty reason, an illegal move or a cycle exits 2, with the legal moves as the hint of an illegal one, and an unknown id exits 3. `tests/golden/ticket-show.json` pins `ticket show --json`.

`factory escalations list [--open]|show <id>|answer <id> --text TEXT|@FILE` reads and answers the escalation records in the trace store ([Escalations](#escalations), [work-items.md](work-items.md#escalations-and-factory-escalations)). Each takes `--json`. `list` prints one `id<TAB>open|answered<TAB>item<TAB>trigger<TAB>first question line` line per record, oldest first, or `{"escalations": [...]}`; `show` prints the record as Markdown with `<details>` sections, or as one JSON document; `answer` stores the answer, prints where the item went, or `{"escalation", "work_item": {"id", "status"}}`, and moves the item to `ready` when no other record on it is open. An empty, too long or second answer, an answer to an item no longer `escalated`, and an unreadable `@FILE` exit 2, and an unknown id exits 3. `tests/golden/escalation-show.json` pins `escalations show --json`.

`factory plan <repo> (--idea TEXT|@FILE | --prd @FILE) [--answer Q=A ...] [--approve] [--ref REF] [--repository NAME] [--plans-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]` takes an idea to tickets in the work-item store, behind a human approval (`pipelines/plan_loop.py`, [plan-loop.md](plan-loop.md)). product-architect writes a PRD that is stored at `data/plans/<run-id>.md` and printed; `--approve` on a second invocation with `--prd @FILE`, with every open question answered by `--answer`, runs story-breakdown and files the tickets in `todo` with parent `prd:<run-id>`, in one `WorkItems.add_many` transaction. It never moves a ticket to `ready`. One pipeline run id covers both agents and the store events, whose actor is `pipeline:plan_loop`. Under `--json`, stdout carries one document with `status` (`awaiting_answers`, `awaiting_approval` or `filed`), `tickets` and `conflicts`. It exits 0 when the PRD waits or the tickets are filed, 1 when `--approve` did not reach `filed` or the PRD is already filed, 2 for usage (an unknown or empty answer included), 3 for a missing repository or file, and 4 when `ANTHROPIC_API_KEY` is not set and a model call is needed. `tests/golden/plan.json` pins the document.

`factory build <id> [--repo PATH] [--dry-run] [--out-dir DIR] [--work-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]` takes one `ready` work item to a reviewed pull request (`pipelines/build_loop.py`, [build-loop.md](build-loop.md)). The feature-coder builds the ticket in a clone behind its file policy, the suite runs in the sandbox and the review panel reviews the diff, with one more coder round when the panel asks for changes. When the panel ships, `github.create_pr` opens the pull request on behalf of the judge and the item moves to `in_review`; a stop a person can answer writes an escalation record and moves it to `escalated`, and every other end after preflight moves it to `blocked` ([Escalations](#escalations)). One pipeline run id covers every agent, the outside write and the store events, whose actor is `pipeline:build_loop`. The pull request's body names the work item, and `github.create_pr` adds the pipeline run id. The report goes to `data/builds/<run-id>.json`, and under `--json` it is the one document on stdout. It exits 0 when the pull request was opened or, in a dry run, would have been, 1 when the run stopped (a refused write, a `blocked` coder, a failed suite, a second request for changes, a budget stop), 2 for usage (an item that is not `ready`, a kind the loop does not take, a dirty repository, an `origin` that is not the ticket's), 3 for an unknown item, agent or ref, and 4 when `ANTHROPIC_API_KEY` or `GH_TOKEN` is not set, a dry run included, or the sandbox is unavailable. `tests/golden/build.json` pins the document.

`factory hotfix <id> [--repo PATH] [--dry-run] [--out-dir DIR] [--work-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]` takes one `ready` incident item to a pull request labelled `hotfix` (`pipelines/hotfix.py`, [hotfix.md](hotfix.md)) and stops there: a person merges and deploys it ([ADR 0004](decisions/0004-no-automated-deploy.md)). It refuses, with exit 2 and before any run, an item whose kind is not `incident` (the hint names `factory build`), that is not `ready`, or whose body does not say `- Reproduced: yes` with test code, the exception, the crash file and a readable Culprit section. The reproduction test must still fail at head the event's way; it is then the finding of the fix loop's stage, reused through `fix_and_review`, which runs the fixer with the test files protected, the suite in the sandbox and the review panel once. When the panel ships, `github.create_pr` opens the pull request on behalf of the judge with the label `hotfix`, and the item goes `ready` to `in_progress` to `in_review`; a stop a person can answer writes an escalation record and moves it to `escalated`, and every other end after preflight moves it to `blocked`, both through the one stop function, with the reason `<ErrorClass>: <message>; report <path>`. The label must already exist on the repository: `github.create_pr` checks it before it pushes, in a dry run too, and refuses a missing one with `OutsideWriteRefused`, exit 1; the factory never creates a label. One pipeline run id covers every agent, the outside write and the store events, whose actor is `pipeline:hotfix`. The report goes to `data/hotfixes/<run-id>.json`, and under `--json` it is the one document on stdout; its `timing` holds the time from the event's last-seen time in the ticket to the pull request. It exits 0 when the pull request was opened or, in a dry run, would have been, 1 when the run stopped, 2 for usage, 3 for an unknown item, agent or ref, and 4 when `ANTHROPIC_API_KEY` or `GH_TOKEN` is not set or the sandbox is unavailable. `tests/golden/hotfix.json` pins the document.

`factory incident <repo> --event JSON|@FILE [--ref REF] [--repository NAME] [--out-dir DIR] [--work-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]` takes one Sentry issue, given as the JSON document `sentry.read_issue` returns (the command never calls Sentry), to an incident ticket (`pipelines/incident.py`, [incident.md](incident.md)). The release is `--ref`, else the event's `release` as a tag or commit, else `v<release>`; without one it exits 2 before any agent runs. In a throwaway clone at that commit, triage-diagnostics diagnoses the event with read-only tools, test-writer writes a reproduction test in `reproduce` mode, and the test runs in the offline sandbox. The test reproduces the event when pytest exits 1 with the event's exception raised from the file of its innermost in-app frame. The pipeline files one `incident` ticket with actor `pipeline:incident`, in `ready` when the event reproduced and in `todo` otherwise. The stack trace, the steps, the suspected paths and the confidence are sections of the body, in the fixed order of `BODY_SECTIONS`, and the suspected paths are also `impacted_paths`; no Ticket field was added. It exits 0 whenever a ticket is filed, 1 when the run fails, 2 for usage, 3 for a missing repository, file, agent or ref, and 4 for a missing key or sandbox. `tests/golden/incident.json` pins the document.

`tests/test_golden_cli.py` pins the `--json` document of `list`, `eval`, `publish-check`, `review`, `triage`, `fix`, `build`, `hotfix`, `incident`, `plan`, `cost`, `deps`, `run`, `ticket show` and `escalations show` against files in `tests/golden/`, with the per-run fields (`duration_ms`, `git_sha`, `pipeline_run_id`, `eval_run_id`, `run_id`) replaced by fixed values. `pytest --update-golden` rewrites those files instead of comparing; read the diff before committing it. A new command gets one test and one file, as `tests/golden/README.md` describes. CI (`.github/workflows/ci.yml`) runs `pytest -m "not live and not live_jev" --cov --cov-fail-under=85`; the floor lives in the workflow command, not in `addopts`, so a local `pytest --cov` never fails on it. `.github/workflows/live.yml` runs the `live` and `live_jev` tests, by hand only (`gh workflow run live.yml`), with the keys from the repository secrets `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY`.

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
