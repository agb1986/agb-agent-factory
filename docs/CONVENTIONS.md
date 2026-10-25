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
| Tool naming, allow and deny | [Tools](#tools) | `tools/registry.py` (#3), `runtime/runner.py` (#5) |
| Fixture repositories | [Fixtures](#fixtures) | `fixtures/build.py`, `fixtures/schema.py` (#9) |
| Gates and routing | [Gates](#gates) | `runtime/gates.py` (#18) |
| Trace row | [Trace row](#trace-row) | `runtime/trace.py` (#6) |
| Secrets | [Secrets](#secrets) | `runtime/runner.py` (#5), `runtime/jev_runner.py` (#17) |
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
| `docs/` | Conventions, plan, Jev spike notes, decisions |

An agent directory is named after the agent: lowercase words joined by hyphens, equal to the manifest's `name`. To keep several versions side by side, put each in a subdirectory named after its full version. That subdirectory name must equal the manifest's `version`. Both layouts may be used for the same agent, and the resolver considers all of them.

```text
agents/code-reviewer/agent.yaml              # one version, here 1.2.0
agents/code-reviewer/system.md
agents/code-reviewer/schemas.py
agents/code-reviewer/evals/cases.yaml
agents/code-reviewer/1.3.0/agent.yaml        # a second version beside it
```

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
| `evals.min_pass_rate` | Eval pass rate the publish gate requires, from 0 to 1 |

Unknown keys are an error, so a typo never falls back to a default. Later tickets add fields and document them here: an optional `guard` agent address (#23), and `limits.max_retries` and `limits.max_tokens_total` (#25).

The resolver validates a manifest as it loads it. `name` is lowercase words joined by hyphens and equals the agent's directory name. Tool names must match the dotted form, and a name in both `allow` and `deny` is an error; an absent `tools` block means no tools. `system_prompt` and `schemas` must be relative paths that stay inside the agent directory and name files that exist. `schemas.py` is imported by file path, and `input_model` and `output_model` must name Pydantic models in it. The resolver does not check that a tool name is known to `tools/registry.py`; the runner does when a run starts, and an unknown name raises `UnknownToolError` (exit 3). The `JevSpec` placeholder in `registry/spec.py` accepts the keys listed under `kind: jev` and validates only the shared fields until #16 completes it.

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

```yaml
name: diff-triage
version: 0.1.0
kind: jev
description: Decides whether a diff needs the review panel and which reviewers see it.
model: jev-model-id-from-docs-jev-sdk   # set by #16 from docs/jev-sdk.md
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

## Agent address

Pipelines and manifests refer to an agent as `name@^MAJOR.MINOR`. The resolver turns the caret range into a `packaging` specifier and picks the highest version that matches:

- `^M.m` with `M` of 1 or more means `>=M.m.0,<(M+1).0.0`. `^1.2` matches 1.2.0 and 1.9.3, but not 1.1.9 or 2.0.0.
- `^0.m` means `>=0.m.0,<0.(m+1).0`, because before 1.0 a minor bump may break callers. `^0.3` matches 0.3.0 and 0.3.7, but not 0.4.0.
- Pre-release versions never match.
- When nothing matches, the resolver raises a `NotFoundError` (exit 3) that lists the versions it found.

`^MAJOR.MINOR` is the only range form; `1.2`, `~1.2`, `^1` and `^1.2.3` raise `InvalidRange` (exit 2). `resolve(name, range)` returns an `LLMSpec` or `JevSpec`. For an LLM agent the attributes #5 reads are `model`, `effort`, `tools.allow`, `tools.deny`, `limits.*`, `system_prompt_file`, and the loaded classes `input_class` and `output_class`; `input_model` and `output_model` stay the class names from the manifest. A missing agent raises `AgentNotFound`; a range with no match raises `VersionNotFound`, whose `available` attribute and hint list the versions found.

A resolved agent is written `name@MAJOR.MINOR.PATCH`, for example `code-reviewer@1.2.0`. That is the form in trace rows and tags.

Any change to a prompt, schema or question bumps the version in the same commit. A change that breaks the input or output model bumps the major. A change that alters behaviour (prompt, questions, model, effort, tools, limits, gates, a compatible schema addition) bumps the minor. A change that never reaches the model (description, eval cases) bumps the patch.

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
- `timeout=limits.timeout_s`, for each request. The runner also checks a deadline of `limits.timeout_s` after each turn, and either one raises `AgentTimeoutError`. The SDK's default of two retries can repeat a request that timed out, so a run can overshoot `timeout_s` before it fails; `limits.max_retries` (#25) will bound that.

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

## Fixtures

A fixture is a directory `fixtures/src/<name>/` holding a `base/` tree, a `head/` tree and a `defects.yaml`; hand-built ones live in `fixtures/handmade/<name>/` in the same shape. `uv run python -m fixtures.build [name ...]` turns each into a git repository under `fixtures/repos/<name>/` (gitignored) with branches `base` and `head`, one commit each. Identity, dates and branch names are fixed and no hooks or git config apply, so the SHAs are reproducible; a rebuild deletes the old repository first. The directory name is the fixture's name and the handle an eval case uses, so a name is never renamed or reused. A new fixture only needs its directory: `discover` picks it up, and `ROOTS` in `fixtures/build.py` lists the source roots.

`defects.yaml` lists the planted defects: `id`, `file`, `line_start`, `line_end` (1-based, inclusive, in the head tree), `severity` (`info`, `minor`, `major` or `blocker`, as in a finding), `category` (a lowercase slug; so far `logic`, `style`, `security`, `performance`, `testing`), `title`, `description` and a `snippet` that must appear inside the window. The list is empty for a fixture a reviewer should pass without findings. The build fails when a window has drifted off its code. Source trees are linted and type-checked with the rest of the repository, so a planted defect must be one ruff and ty accept.

```yaml
fixture: off-by-one
summary: Running-sum rewrite that drops the last window.
defects:
  - {id: last-window-dropped, file: series/rolling.py, line_start: 12, line_end: 13,
     severity: major, category: logic, title: ..., description: ..., snippet: "len(values) - 1"}
```

## Gates

Thresholds, skip decisions and routing are code in `runtime/gates.py`. The thresholds come from the manifest, never from a prompt, and no model decides them. A gate evaluation returns a `Decision`:

```python
@dataclass(frozen=True, slots=True)
class Decision:
    skip: bool  # skip the full review
    escalate: bool  # hand the decision to Claude or a human
    route: tuple[str, ...]  # agent names to run, in order, without duplicates
    reason: str  # gate name, or "truncated" or "jev_error"


Decision(skip=False, escalate=True, route=("code-reviewer", "security-reviewer"), reason="unsure")
```

`route` is a tuple so that a frozen `Decision` really cannot change. These rules hold whatever the gates say:

- An escalation clears `skip`.
- A truncated input yields `skip=False` with reason `truncated`.
- A Jev failure escalates with reason `jev_error`.
- Jev never has the last word on anything consequential.

## Trace row

Every agent run, successful or not, writes exactly one row to the DuckDB trace store. The default path is `data/traces.duckdb`, overridden by an argument or by `FACTORY_TRACE_DB` (#6).

The LLM runner writes the row in a `finally` block, so a failed run still records its usage and `error` as `<ErrorClass>: <message>`. Usage is summed over every response, the validation retry included. Two checks come before the run starts and write no row, because there is nothing valid to record: a payload that fails the input model, and a missing `repo_path` for an agent that allows tools. Both raise `UsageError`.

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
| `decision_json` | JSON, null | The `Decision` and gate inputs for a Jev or guard run |
| `input_json` | JSON | The validated input payload |
| `output_json` | JSON, null | The validated output, or the full probability vectors for Jev; null on failure |
| `error` | VARCHAR, null | Error class and message when the run failed |
| `tag` | VARCHAR, null | Set by the eval runner and publish gate, e.g. `eval:code-reviewer@1.2.0` |

A `Trace` holds one DuckDB connection for its lifetime and opens it on first use, rather than opening per call. DuckDB allows one read-write process per file, so holding the connection keeps the single writer (#26) in charge of the file and saves a file open on every run; close it with `close()` or a `with` block. Schema changes are numbered migrations in `runtime/trace.py`: append a `(version, sql)` pair, never edit a shipped one, and `ensure_schema()` applies the missing ones and records them in `schema_version`. `cost_usd` is null for a model with no entry in `runtime/pricing.py`, and aggregates treat null as zero.

## Secrets

`ANTHROPIC_API_KEY` and the Jev key are read from the environment and from nowhere else. The Jev key is `TYPESAFE_API_KEY`, confirmed by #15 in [jev-sdk.md](jev-sdk.md).

- Store keys with `agb sys secrets set <NAME>`, which writes them to a mode-600 `secrets.env`.
- The factory never reads that file. Whoever starts the factory exports the key: the shell, CI, or the `agb factory` wrapper (#30).
- A missing key raises `AuthOrConfigError` (exit 4) with a hint that names the variable, never its value. So does a key the API rejects with a 401 or 403, with the same hint.
- No secret goes into the repository, a test, a config file, a container image, a trace row, a log line or an error message.
- Tests that need a live key carry the `live` marker; `tests/conftest.py` skips them when `ANTHROPIC_API_KEY` is unset. #17 extends that hook so Jev live tests skip without the Jev key.

```python
if not os.environ.get("ANTHROPIC_API_KEY"):
    raise AuthOrConfigError(
        "ANTHROPIC_API_KEY is not set",
        hint="store it with `agb sys secrets set ANTHROPIC_API_KEY` and export it",
    )
```

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

Later typed errors subclass one of these. A resolver miss (`AgentNotFound`, `VersionNotFound`) is a `NotFoundError`; a missing key is an `AuthOrConfigError`; `AgentError`, `JevError`, `GuardError` and `BudgetExceeded` are `FactoryError`s.

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
