# Execution plan: models, waves and agent prompts

Companion to [PLAN.md](PLAN.md). PLAN.md says what to build; this file says how to run the build with coding agents: which model drives each issue, which issues run in parallel, and the prompt each agent starts from. The issue tracker stays the source of truth for status; update this file only when the dependency graph changes.

The models named here are the models that drive the coding agent (Claude Code session, Orca worktree, or similar). The models that the factory's own agents call at runtime are fixed by the issues themselves (`claude-sonnet-5-5` for code-reviewer, `claude-opus-5-5` for judge, `claude-haiku-5-5` for triage fallback and action-guard) and are not a choice made here.

## 1. Model per issue

Rule of thumb, applied below:

- **Opus 5.5, effort `xhigh`** for tickets where the agent decides an interface that other tickets inherit, where the API surface has drifted recently and a stale prior is the main risk, or where the deliverable is a judgement call (spike, decision record, calibration). Mistakes here propagate.
- **Sonnet 5.5, default effort** for well-specified build tickets with a clear acceptance test. The issue text is already the spec.
- **Haiku 5.5** only for mechanical work with no design content.
- **Fable 5.1** is the fallback if an Opus lane stalls or produces a runtime that fails its own live smoke twice. Do not start there; it is 2.5x the price of Opus and the issues are specified well enough that Opus should land them.

| Issue | Title (short) | Model | Why |
| --- | --- | --- | --- |
| #1 | Conventions doc + exit codes | Opus 5.5 | Fixes the manifest schema, output contract and trace shape that #3 to #7 implement. Design, not typing. |
| #2 | Scaffold package | Sonnet 5.5 | Known shape: pyproject, uv, ruff, ty, pytest, pre-commit, console entry point. |
| #3 | Tools registry | Sonnet 5.5 | Clear spec and clear tests (traversal, symlinks, byte limit). Security-sensitive but mechanical. |
| #4 | Registry resolver | Sonnet 5.5 | Pydantic models plus semver resolution; tests enumerated in the issue. |
| #5 | LLM runtime on Tool Runner | Opus 5.5 | Foundation for every LLM agent. Tool Runner, structured outputs, adaptive thinking, caching, refusal handling: all recently changed API surface. |
| #6 | Trace store + pricing | Sonnet 5.5 | DuckDB schema, idempotent migration, two aggregate queries. |
| #7 | code-reviewer@1.2 end to end | Sonnet 5.5 | First consumer of #3 to #6; mostly wiring plus a prompt and four eval cases. |
| #8 | Eval runner | Sonnet 5.5 | Spec is precise: cases.yaml shape, deterministic checks then llm_judge, thread pool, pass-rate exit code. |
| #9 | Fixture repositories | Sonnet 5.5 | Deterministic git repos with planted defects. Care needed in the defects, not in the architecture. |
| #10 | Publish gate | Sonnet 5.5 | Diff against base branch, record in trace, resolver flag. |
| #11 | judge@2.0 | Opus 5.5 | Judge prompt and score schema decide what "ship" means for the whole panel; weights in code need thought about edge cases. |
| #12 | security-reviewer@1.0 | Sonnet 5.5 | Same shape as #7 with a different checklist and fixtures. |
| #13 | perf-reviewer@0.3 | Sonnet 5.5 | Same shape as #7. |
| #14 | review_panel pipeline | Sonnet 5.5 | Composition of existing pieces; parallelism, pipeline run id, CLI, exit codes. |
| #15 | Jev SDK spike | Opus 5.5 | Research under a timebox with a go/no-go that closes or opens a whole phase. Needs judgement about evidence quality. |
| #16 | JevSpec | Sonnet 5.5 | Pydantic validation rules and generated models, all enumerated. |
| #17 | jev_runner.py | Sonnet 5.5 | Transport behind an interface, retries, timeout, mocked tests. Shape fixed by #15. |
| #18 | gates.py + triage route-only | Sonnet 5.5 | `Decision` dataclass, wiring into the panel, one CLI command. |
| #19 | judge-gate + Jev grader | Sonnet 5.5 | Second Jev agent; follows the #16 to #18 pattern. |
| #20 | Calibration and shadow mode | Opus 5.5 | Labelling 100+ diffs, threshold selection against a recall floor, and deciding when skip is safe. This is the one ticket where a wrong call silently drops real defects. |
| #21 | Fixer runtime decision | Opus 5.5 | Three-way architecture decision with a prototype; the record is read for years. |
| #22 | test-writer@0.1 and fixer@0.1 | Sonnet 5.5 | Two agents on the runtime chosen in #21, with limits and structured output. |
| #23 | action-guard hook | Sonnet 5.5 | Hook on the per-turn interface from #5; refusal semantics are spelled out in the issue. |
| #24 | fix_loop pipeline | Opus 5.5 | Sandbox, throwaway worktree, resource limits, six-stage flow. Most ways to get this subtly wrong of any pipeline. |
| #25 | Retries and cost guard | Sonnet 5.5 | Limits in manifest, budget check, one CLI command. |
| #26 | Async runtime | Opus 5.5 | Touches the runtime core; single-writer trace under concurrency is where races hide. |
| #27 | CI and test harness | Sonnet 5.5 | GitHub Actions, coverage floor, golden JSON tests. |
| #28 | Deployment | Sonnet 5.5 | Dockerfile, Compose, volume, env-file secrets, deploy doc. |
| #29 | Release hygiene | Haiku 5.5 | CHANGELOG, version from metadata, tag. Mechanical. |
| #30 | `agb factory` wrapper | Sonnet 5.5 | Lives in agb-tools; needs that repo's conventions, which Haiku tends to skip. |

## 2. Waves

Waves are groups whose members have all their dependencies merged and touch disjoint files. Run every lane in a wave in parallel (one worktree per lane), merge the wave, start the next. Two cross-phase moves shorten the critical path: the Jev spike (#15) and the fixtures (#9) run early because nothing in Phase 1 blocks them, and the fixer decision (#21) runs alongside Phase 2 because it needs only the runtime and one fixture.

| Wave | Lanes (parallel) | Needs merged | Notes |
| --- | --- | --- | --- |
| 1 | #2 scaffold, #1 conventions | nothing | Merge #2 first; #1 rebases onto it before running its exit-code test. #1 owns `docs/CONVENTIONS.md`, `factory/exit.py` and its test; #2 owns everything else. |
| 2 | #3 tools, #4 resolver, #6 trace, #15 Jev spike | 1, 2 | Four disjoint directories: `tools/`, `registry/`, `runtime/trace.py` + `runtime/pricing.py`, `docs/jev-sdk.md`. |
| 3 | #5 runtime, #9 fixtures | 3, 4, 6 (for #5); 2 (for #9) | #9 writes only under `fixtures/` and `.gitignore` is already set. |
| 4 | #7 code-reviewer, #16 JevSpec | 5, 9 (for #7); 4, 15 go (for #16) | If #15 is no-go, drop #16, #17, #18, #19, #20 from every later wave and replace triage with the Haiku LLM agent noted in #15. |
| 5 | #8 eval runner, #21 fixer decision, #17 jev_runner | 7, 9 (for #8); 5, 9 (for #21); 6, 16 (for #17) | #21 is a two-day timebox including the prototype; it does not block wave 6. |
| 6 | #10 publish gate, #11 judge, #12 security-reviewer, #13 perf-reviewer | 8 (for #10); 7, 8, 9 (for #11 to #13) | Three agent directories plus `registry/` and `.pre-commit-config.yaml`. No overlap. |
| 7 | #14 review_panel, #22 test-writer + fixer, #23 action-guard | 10 to 13 (for #14); 21, 8, 9 (for #22); 5, 17 (for #23) | #23 adds a hook to `runtime/runner.py`; #14 only calls `run()`. Agree the hook signature from #5 before starting. |
| 8 | #18 triage route-only, #25 retries + cost guard, #27 CI | 14, 17 (for #18); 14 (for #25); 10, 14 (for #27) | #18 and #25 both add a `factory` subcommand. Each adds its own module under `factory/commands/` and one registration line; expect one trivial conflict. |
| 9 | #19 judge-gate, #26 async runtime, #28 deployment | 18 (for #19); 25 (for #26); 27 (for #28) | #26 keeps the sync API; #19 and #28 do not touch the runtime. |
| 10 | #24 fix_loop, #20 calibration (start) | 19, 22, 23, 26 (for #24); 18 (for #20) | #20 then runs two weeks of shadow mode. It is a lane that stays open while wave 11 happens. |
| 11 | #29 release v0.1.0 | 24, 28, and the handover DoD | Tag only when the DoD in HANDOVER-v2.md section 6 is met. |
| later | #30 `agb factory` wrapper | daily use of the factory CLI | Separate repository. Not before the CLI has been used for real work for a while. |

Critical path: #2, #1, #4, #5, #7, #8, #11, #14, #18, #19, #24, #29. Everything else floats.

Before each wave: `git checkout main && git pull`, create one worktree per lane (`git worktree add ../factory-issue-N -b issue-N-<slug>`), start the agent in that worktree with the prompt below. After the wave: review each PR, merge in the order the Notes column implies, delete worktrees, run `uv run pytest` on main once. The coordinator also runs the live suite once per wave, pre-merge, on main: `FACTORY_LIVE=1 uv run pytest -m "live or live_jev"` with the keys set (or `gh workflow run live.yml`). Workers never run it.

## 3. Prompts

### 3.1 Common preamble

Every prompt below begins with this block. Paste it first, then the issue-specific section.

```text
You are working in the repository agb-agent-factory (Python 3.14, uv). You are in a
git worktree on the branch for one GitHub issue. Deliver that issue completely and
nothing else.

Read, in this order, before writing code:
1. docs/PLAN.md (decisions, stack, layout, conventions, rules)
2. docs/CONVENTIONS.md if it exists (fixed conventions; follow them exactly)
3. the issue: `gh issue view <N>` (the acceptance criteria there are the spec)
4. docs/HANDOVER-v2.md only for background if something in the issue is unclear

Hard rules:
- Never pass `temperature`, `top_p` or `top_k` to the Anthropic API. Use
  `thinking: {"type": "adaptive"}` and `output_config.effort` from the manifest.
- Structured outputs go through `output_config.format`; never force `tool_choice`
  to extract output.
- Model ids, prices and limits are configuration. Verify any model id against
  `client.models.list()` in a live test before committing it; never assume one
  from memory. Prices carry the date they were checked in a comment.
- Secrets come from the environment (`ANTHROPIC_API_KEY`, the Jev key) or from
  `agb sys secrets`. Nothing secret in the repository, tests, or an image.
- Tests that need a live key are marked `live` or `live_jev`. They are deselected unless
  `FACTORY_LIVE=1` is set, and skipped when that key is absent. Never set `FACTORY_LIVE`;
  the coordinator runs the live suite once per wave.
- Orchestrated workers write traces to one shared store:
  `export FACTORY_TRACE_DB=~/.local/share/factory/traces.duckdb` before any run, so
  `factory cost --since` covers every worker and the rows outlive the worktree.
- Tools in `tools/` are read-only unless the issue says they run in the sandbox.
- Gates, thresholds and routing live in `runtime/gates.py` and return a
  `Decision`; never inside a prompt.
- One validation retry on a bad structured output, then the run fails with a
  typed error. No silent fallbacks.
- Do not edit files outside the ownership list in your issue section unless the
  issue requires it; if you must, say so in the PR description.

Quality bar (definition of done for every ticket):
- `uv run pytest` passes; `uv run ruff check . && uv run ruff format --check .`
  and `uv run ty check` are clean; `pre-commit run --all-files` is clean.
- Public functions and classes have docstrings.
- Any new convention you had to invent is recorded in docs/CONVENTIONS.md in one
  short paragraph under the right heading.
- For agents: manifest validates, output round-trips through structured outputs,
  at least four eval cases, a trace row with cost, version bumped.
- For Jev agents with gates, the eval also reports `recall_risky`, `skip_rate`
  and `escalation_rate`, and fails below `evals.min_recall_risky` when the
  manifest sets it (docs/CONVENTIONS.md, "Eval cases").

Finish:
- Commit in small logical commits. Commit messages are normal prose with a
  conventional-commit prefix (feat, fix, docs, test, chore). End each with
  `Co-Authored-By: Claude <model name> <noreply@anthropic.com>`.
- Never put a `Claude-Session:` trailer or a claude.ai session URL anywhere.
- Open a PR against main with `gh pr create`. The body states what was built,
  how it was verified (paste the decisive command output, not the whole log),
  what was left out and why, and ends with `Closes #<N>` and then the line
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- If you are blocked by a decision only the repository owner can make, stop,
  write what you know and the options in the PR description as a draft PR, and
  say so in your final message.
```

### 3.2 Per-issue sections

#### #1 Conventions and exit codes (Opus 5.5)

```text
Issue: #1. Branch: issue-1-conventions.
You own: docs/CONVENTIONS.md, factory/exit.py, tests/test_exit.py.

Goal: write the conventions every later ticket implements, and the one piece of
code that every CLI command shares.

Write docs/CONVENTIONS.md with one short section per convention, each stating the
rule and a minimal example. Cover, in this order: repository layout (as in
PLAN.md); the manifest schema for `kind: llm` (name, version, model, effort,
system prompt path, schemas module, input/output model names, tools allow and
deny lists, limits block with max_tool_turns, timeout_s, max_output_tokens) and
for `kind: jev` (model, input.render, max_chars, questions, gates, routing,
evals); the agent address `name@^major.minor` and how a caret range maps to a
version match; the output contract (exactly the output model via structured
outputs, one validation retry, then typed failure); forbidden request fields
(temperature, top_p, top_k) and required ones (adaptive thinking, effort from the
manifest); tool naming (dotted names in manifests, `__` on the wire) and the
allow/deny rule including rejection of an allow and deny intersection; gates as
code returning `Decision(skip, escalate, route, reason)`; the trace row shape
(run id, pipeline run id, agent ref, kind, started at, duration ms, input,
output, cache read and cache write tokens, cost_usd, decision JSON, input JSON,
output JSON, error); secrets; and the CLI shape (`factory <verb> [args] [--json]`,
`--json` prints exactly one JSON document on stdout, humans get prose on stderr).

Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4 auth or config missing.
Implement them as an IntEnum in factory/exit.py with a docstring per member and a
helper that maps the known exception types to a code. Test that every code is
distinct, that the mapping is total for the exception types it claims, and that
unknown exceptions map to 1.

Acceptance: one section per convention listed above; the exit-code test passes.
This ticket runs in parallel with #2. If factory/ does not exist yet when you
start, create only factory/exit.py and the test; rebase onto main once #2 merges
and confirm the test runs under the project's pytest configuration.
```

#### #2 Scaffold package (Sonnet 5.5)

```text
Issue: #2. Branch: issue-2-scaffold.
You own: pyproject.toml, uv.lock, .pre-commit-config.yaml, factory/__init__.py,
factory/cli.py, tests/conftest.py, tests/test_cli.py, README.md (stack section
only), .python-version.

Goal: a package that installs with uv, lints, type-checks, tests, and exposes a
`factory` console command.

Build: pyproject with the uv build backend, `requires-python = ">=3.14"`, runtime
deps `anthropic`, `pydantic>=2`, `pyyaml`, `packaging`, `duckdb`, optional extra
`jev` (empty for now with a comment pointing at #15), dev group `ruff`, `ty`,
`pytest`, `pytest-cov`, `pre-commit`. Ruff line length 100, select the default
rules plus isort and pyupgrade. Pre-commit runs ruff check, ruff format and ty.
`factory --version` prints the version read from package metadata;
`factory list` prints the agents found under agents/ (empty list is fine; use a
stub that #4 will replace, and say so in a comment). Exit codes come from
factory/exit.py once #1 merges; until then return 0 and 2 only.

Check and record in the PR body which `anthropic` version installed and whether
it pulls `httpx2`, `httpx`, or both. That answers an open question in PLAN.md;
add one line under its Open questions if the answer is clear.

Acceptance: fresh clone, `uv sync && uv run factory --version` prints a version;
`uv run pytest` passes; `pre-commit run --all-files` is clean.
```

#### #3 Tools registry (Sonnet 5.5)

```text
Issue: #3. Branch: issue-3-tools-registry.
You own: tools/__init__.py, tools/registry.py, tools/git.py, tools/rg.py,
tests/tools/.

Goal: the scoped, read-only tools that LLM agents may call, and the helper that
turns an agent's allow and deny lists into tool definitions.

Build `TOOLS: dict[str, tuple[dict, Callable]]` keyed by dotted name with
`git.read_file`, `git.show_diff`, `rg.search`. Each entry is a JSON-schema tool
definition plus the implementation. Every tool takes a repository root and only
reads inside it: resolve the real path, reject anything that escapes the root,
including through symlinks, with a typed error that names the offending path.
Subprocesses run as argv lists, never through a shell, with `git` and `rg` from
PATH. Output is capped at a byte limit from the registry's configuration; when
truncated, append a single marker line that says so and how many bytes were
dropped. `tool_defs(allow, deny)` returns the wire-format definitions (dotted
names rewritten to `__`), raises if any name is unknown, and raises if allow and
deny overlap. A helper maps a wire name back to the dotted name for the runtime.

Tests use a temporary git repository built in a fixture: a commit, a modified
file, a symlink pointing outside. Cover traversal (`..`, absolute path, symlink),
each tool's happy path, truncation marker, allow/deny intersection, and unknown
tool name.

Acceptance: all of the above tested; no tool writes or runs anything that
mutates the repository.
```

#### #4 Registry resolver (Sonnet 5.5)

```text
Issue: #4. Branch: issue-4-resolver.
You own: registry/__init__.py, registry/spec.py, registry/resolver.py,
tests/registry/, and the `factory list` implementation in factory/cli.py.

Goal: load, validate and resolve agent manifests by name and caret range.

Build Pydantic models: a shared `AgentSpecBase` (name, version, kind,
description, evals block with min_pass_rate) and `LLMSpec` (model, effort,
system_prompt path, schemas path, input_model, output_model, tools allow and
deny, limits) and a `JevSpec` placeholder that accepts the fields listed in
docs/CONVENTIONS.md but validates only the shared ones (#16 completes it).
`resolve(name, range) -> AgentSpec` finds manifests at agents/<name>/agent.yaml
and agents/<name>/<version>/agent.yaml, picks the highest version matching the
caret range using `packaging`, and raises a typed error that lists the available
versions when none match. Loading an LLMSpec imports schemas.py by path and
checks that the named input and output classes exist and are Pydantic models.
`list_agents()` returns every (name, version, kind). Wire it into `factory list`
and `factory list --json`.

Tests: caret to specifier conversion (`^1.2` matches 1.2.0 and 1.9.3, not 2.0.0),
highest-match selection across both layouts, missing agent, missing schema
class, unknown kind, malformed YAML, version in manifest disagreeing with the
directory name.

Hand-off: #5 calls `resolve()` and reads `spec.model`, `spec.effort`,
`spec.tools`, `spec.limits`, and the loaded input and output model classes.
Expose those as attributes, not as a dict.
```

#### #5 LLM runtime (Opus 5.5)

```text
Issue: #5. Branch: issue-5-llm-runtime.
You own: runtime/__init__.py, runtime/runner.py, runtime/errors.py,
runtime/hooks.py, tests/runtime/test_runner.py, tests/live/test_runner_live.py.

Goal: `run(spec, payload, trace) -> output_model` for `kind: llm` agents on the
Anthropic SDK Tool Runner, with structured outputs, prompt caching, limits and
a per-turn hook interface.

Before coding, load the claude-api skill or read the SDK's Tool Runner and
structured-output docs; the API changed in 2026 and a stale prior is the main
risk here. Use `client.beta.messages.tool_runner` with the tools from
`tools.tool_defs(spec.tools.allow, spec.tools.deny)`. Put the output model in
`output_config.format`, `effort` from the manifest, `thinking: {"type":
"adaptive"}`; pass thinking blocks back unchanged on every turn. Mark the
system prompt with `cache_control`. Enforce `limits.max_tool_turns`,
`limits.timeout_s`, `limits.max_output_tokens`. On a structured output that
fails Pydantic validation, send the validation error back once and retry;
on a second failure raise `AgentError` carrying the partial transcript. Handle
stop reasons `refusal` and `max_tokens` explicitly as typed errors. Accumulate
usage (input, output, cache read, cache write) across turns and write one trace
row via the trace handle from #6, including cost from runtime/pricing.py.

Hook interface: a small protocol with `before_tool(call) -> ToolDecision` and
`after_tool(call, result)` that the runner invokes around every tool execution;
`ToolDecision` can allow, or deny with a message that becomes the tool result.
The default hook allows everything. #23 plugs in here; keep the signature small
and documented in docs/CONVENTIONS.md.

Tests (mocked SDK): happy path straight to output; one tool call then output;
validation failure then success on retry; validation failure twice raises
AgentError with transcript; turn limit; refusal stop reason; hook deny becomes
tool result. One live test, skipped without ANTHROPIC_API_KEY, runs a trivial
agent twice and asserts `cache_read_input_tokens` is non-zero on the second run.

Hand-off: #7 needs `run()` to accept the resolved spec and a dict or model
payload and return the output model instance. #14 needs a `pipeline_run_id`
argument that lands in the trace row.
```

#### #6 Trace store and pricing (Sonnet 5.5)

```text
Issue: #6. Branch: issue-6-trace.
You own: runtime/trace.py, runtime/pricing.py, tests/runtime/test_trace.py,
tests/runtime/test_pricing.py.

Goal: one DuckDB row per agent run with cost, and the price table that
computes it.

Build `Trace` with `ensure_schema()` (idempotent; a `schema_version` table and
numbered migrations), `record(row)`, `cost_by_agent(since=None)`,
`cost_by_pipeline_run(pipeline_run_id)`. Columns follow docs/CONVENTIONS.md:
run_id, pipeline_run_id, agent_ref, kind, started_at, duration_ms,
input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd,
decision_json, input_json, output_json, error, plus a `tag` column for the eval
runner (#8) and publish gate (#10). Default path data/traces.duckdb, overridable
by argument and by an environment variable. Open the database per call or hold
one connection per Trace instance; document which and why (#26 needs a single
writer).

Pricing: a dict of per-million-token prices per model id for input, output,
cache read and cache write, with the date checked in a comment on each entry.
`cost_usd(model, usage) -> float | None`: unknown model returns None and logs a
warning once per process per model. Include the four current Claude model ids
with their list prices and a note that the live `client.models.list()` call in
#7 is where ids are verified.

Tests: ensure_schema twice is a no-op, record then both aggregates, unknown
model yields null cost and one log line, schema_version increments with a second
migration in a test.
```

#### #7 code-reviewer@1.2 end to end (Sonnet 5.5)

```text
Issue: #7. Branch: issue-7-code-reviewer.
You own: agents/code-reviewer/ (agent.yaml, system.md, schemas.py,
evals/cases.yaml), tests/agents/test_code_reviewer.py,
tests/live/test_code_reviewer_live.py.

Goal: the first real agent, running through resolver, runtime, tools and trace
against the live API.

Manifest: name code-reviewer, version 1.2.0, kind llm, model claude-sonnet-5-5,
effort high, tools allow git.read_file, git.show_diff, rg.search, deny empty,
limits max_tool_turns 12, timeout_s 180, max_output_tokens 4096, evals
min_pass_rate 0.75. schemas.py defines ReviewInput (repo_path, base, head,
optional focus), Finding (file, line_start, line_end, severity in
info/minor/major/blocker, category, title, body, suggested_fix optional) and
ReviewOutput (findings, summary, verdict in approve/request_changes). system.md
tells the model to read the diff first, open files only when the diff is
insufficient, report line numbers from the head revision, and never invent a
line it has not seen. Write four eval cases referencing fixtures by name
(off-by-one, clean-refactor, untested-change, mixed; #9 builds them, use the
names from that issue). If #9 has not merged, hand-build the off-by-one fixture
under fixtures/handmade/ with a small script and make the smoke test use it.

Live smoke test (skipped without ANTHROPIC_API_KEY): run the reviewer on the
off-by-one fixture; assert at least one finding at the planted file with
severity major or blocker and a line window covering the defect; run it a second
time and assert the trace row shows non-zero cache read tokens and a non-null
cost. Also assert in the live test that `claude-sonnet-5-5` appears in
`client.models.list()`.

Acceptance: both runs traced under distinct run ids; the PR body includes the
two trace rows with cost.
```

#### #8 Eval runner (Sonnet 5.5)

```text
Issue: #8. Branch: issue-8-eval-runner.
You own: evals/__init__.py, evals/run.py, evals/graders/__init__.py,
evals/graders/llm_judge.py, evals/cases.py (schema), tests/evals/,
the `factory eval` subcommand.

Goal: `evals/run.py <agent>[@range]` runs an agent's cases and exits non-zero
below its min_pass_rate.

Case schema (Pydantic, validated on load): id, fixture (name resolved under
fixtures/repos/<name>), payload (merged into the agent's input model), expected
with planted defects (file, line window, severity floor) and verdict, grader
(`llm_judge` now, `jev` accepted but raises NotImplemented until #19). Grading:
deterministic checks first (every planted defect matched by some finding whose
file matches and whose line window overlaps; verdict matches), then the
llm_judge grader, a small structured-output call on claude-haiku-5-5 that
receives expected and actual and returns pass, score 0 to 1 and a reason.
Deterministic failure short-circuits the LLM call. Each case run is traced with
tag `eval:<agent>@<version>` so cost_by_agent can separate eval spend. Cases run
in a thread pool sized by `--workers` (default 4). Output is a table on stderr
and, with `--json`, one JSON document on stdout. Exit 0 at or above
min_pass_rate, 1 below, 2 on usage, 3 when the agent or a fixture is missing.

Tests: gate both ways with a fake runner (pass rate above and below), case
schema validation errors, deterministic short-circuit skips the judge, JSON
output shape. Golden file for the JSON output, which #27 reuses.
```

#### #9 Fixture repositories (Sonnet 5.5)

```text
Issue: #9. Branch: issue-9-fixtures.
You own: fixtures/build.py, fixtures/README.md, fixtures/manifest schema,
fixtures/src/<name>/ (source trees and defect manifests), tests/fixtures/.

Goal: deterministic small git repositories with planted defects that evals
reference by name.

Build a Python script (preferred over bash for Windows-free portability and
testability) that creates, under fixtures/repos/<name>/ (gitignored), a git
repository with a `base` and a `head` ref, from source trees committed under
fixtures/src/. Fixed author, committer, dates and no hooks so the SHAs are
reproducible; re-running wipes and rebuilds; whole build under 10 seconds.
Fixtures: off-by-one (a loop bound bug in a Python function with a test that
passes on base and does not cover the bug), clean-refactor (rename plus extract
method, behaviour identical), untested-change (new public function, no test),
mixed (one real bug, one style nit, one harmless change in three files), plus
triage fixtures docs-only, test-only, sql-injection (string-formatted query),
n-plus-one (query inside a loop). Each fixture has a manifest
(fixtures/src/<name>/defects.yaml) listing planted defects with file, line
window, severity and category; an empty list for the clean ones. Also build a
hand-built single-file off-by-one under fixtures/handmade/ if #7 did not.

Tests: build twice and compare SHAs; every defects.yaml validates; every
referenced file and line window exists in the head tree.

Hand-off: evals/cases.yaml in agents reference fixtures by directory name.
Document the names in fixtures/README.md and do not rename them later.
```

#### #10 Publish gate (Sonnet 5.5)

```text
Issue: #10. Branch: issue-10-publish-gate.
You own: scripts/check_version_bump.py, the pre-commit hook entry, the
`factory publish-check` subcommand, registry/published.py, tests for each.

Goal: no prompt, schema or question change ships without a version bump, and
pipelines run only published agent versions.

Build a script that diffs agents/*/system.md, agents/*/schemas.py and the
`questions` block of agents/*/agent.yaml against the base branch (argument,
default origin/main) and fails listing every agent whose content changed
without a version change. Register it in pre-commit and expose it for CI (#27
wires it). `factory publish-check <agent>[@range]` runs the eval runner from
#8, and on pass writes a row to a `published` table in the trace store (agent,
version, pass_rate, date, git sha) via a new Trace method; on fail exits 1 and
writes nothing. The resolver gains `published_only: bool` that, when true,
ignores versions without a published row; pipelines will pass true unless
`--allow-unpublished` is set (#14 consumes this flag; add it to the resolver
now and document it).

Tests: changed prompt without bump fails, with bump passes, unchanged passes;
publish-check writes exactly one row on pass and none on fail (fake eval
runner); published_only resolution skips an unpublished higher version.
```

#### #11 judge@2.0 (Opus 5.5)

```text
Issue: #11. Branch: issue-11-judge.
You own: agents/judge/, runtime/scoring.py, tests/agents/test_judge.py,
tests/runtime/test_scoring.py.

Goal: the agent that merges reviewer output into one verdict, with the arithmetic
in code and the judgement in the model.

Schemas: JudgeInput (reviews keyed by reviewer address, failed_reviewers list
with error strings, diff_context with base, head and the unified diff capped by
the runtime's byte limit), JudgeOutput (scores: correctness_risk,
maintainability, security_risk, test_coverage, each 0 to 1 with a one-line
justification; merged_findings as a deduplicated list of Finding from
code-reviewer's schema with a `sources` list; rationale). system.md instructs the
model to trust a finding only if it can confirm the cited line exists via
git.read_file, to downgrade or drop findings pointing at lines that do not
exist, to prefer the stricter severity when reviewers disagree on the same line,
and to say explicitly when a failed reviewer leaves a blind spot. Manifest:
version 2.0.0, model claude-opus-5-5, effort high, tools allow git.read_file
only, limits max_tool_turns 10.

runtime/scoring.py: `weighted_score(scores, weights) -> float` and
`ship(score, threshold) -> bool`, weights and ship_threshold read from the judge
manifest under a `scoring` block, validated to sum to 1. The agent never emits
`ship`; the pipeline computes it.

Evals (four cases, llm_judge grader until #19): unanimous reviewers yield agree;
two reviewers disagree on severity, judge picks the stricter with reason; a
reviewer cites a line that does not exist and the judge downgrades it; one
reviewer failed and the rationale names the gap. Unit tests for weighted_score
and ship, including weights that do not sum to 1 rejected at manifest load.
```

#### #12 security-reviewer@1.0 (Sonnet 5.5)

```text
Issue: #12. Branch: issue-12-security-reviewer.
You own: agents/security-reviewer/, tests/agents/test_security_reviewer.py.

Goal: a reviewer focused on injection, auth, secrets, deserialisation and
dependency risk, in the exact shape of code-reviewer.

Reuse ReviewInput, Finding and ReviewOutput by importing from
agents/code-reviewer/schemas.py (or move the shared models to
registry/common_schemas.py and re-export; say which in the PR). Manifest
version 1.0.0, model claude-sonnet-5-5, effort high, tools allow git.read_file,
git.show_diff, rg.search. system.md gives a checklist (SQL and command
injection, missing authorisation checks, hard-coded credentials and tokens,
unsafe deserialisation, dependency pins with known advisories, path traversal,
weak randomness for secrets) and tells the model to use rg.search to find other
call sites before deciding a sink is reachable, and to set category to one of a
fixed list. Evals: sql-injection fixture (blocker at the formatted query),
clean-refactor (no findings above info), a hard-coded secret fixture (add it to
fixtures/src/ with a defects.yaml, following #9's layout), a path traversal
fixture (same). Add both new fixtures to fixtures/build.py.

Acceptance: eval pass rate at or above the manifest threshold in a live run;
trace rows with cost in the PR body.
```

#### #13 perf-reviewer@0.3 (Sonnet 5.5)

```text
Issue: #13. Branch: issue-13-perf-reviewer.
You own: agents/perf-reviewer/, tests/agents/test_perf_reviewer.py, and the two
new fixtures below under fixtures/src/.

Goal: a reviewer for algorithmic complexity, N+1 queries, unbounded memory and
blocking calls on hot paths, in the shape of code-reviewer.

Reuse the shared review schemas the same way #12 does (coordinate by reading
#12's PR if it has merged; otherwise import from code-reviewer's schemas.py).
Manifest version 0.3.0, model claude-sonnet-5-5, effort high, same tools as
code-reviewer. system.md tells the model to estimate complexity in terms of the
input that grows, to look for loops that issue queries or HTTP calls, to flag
collections that grow without bound in long-lived processes, and to mark
findings minor unless the path is clearly hot. Evals: n-plus-one fixture
(major), clean-refactor (nothing above info), quadratic-loop (new fixture: a
nested loop over the same list where a set would do), missing-index-hint (new
fixture: a query on an unindexed column inside a request handler, with a
migration file nearby showing no index). Add fixtures to fixtures/build.py with
defects.yaml.

Acceptance: eval pass rate at or above threshold in a live run; trace rows with
cost in the PR body.
```

#### #14 review_panel pipeline (Sonnet 5.5)

```text
Issue: #14. Branch: issue-14-review-panel.
You own: pipelines/__init__.py, pipelines/review_panel.py, pipelines/ids.py,
the `factory review` subcommand, tests/pipelines/test_review_panel.py,
tests/live/test_review_panel_live.py.

Goal: run the three reviewers in parallel, hand the results to the judge, and
emit one JSON result under one pipeline run id.

Resolve code-reviewer@^1.2, security-reviewer@^1.0, perf-reviewer@^0.3 and
judge@^2.0 up front with published_only unless --allow-unpublished, and fail
fast with exit 3 if any is missing. Generate a pipeline_run_id and pass it to
every run() so all four trace rows share it. Run reviewers in a thread pool
(the sync runtime; #26 makes it async). A reviewer that raises is recorded in
failed_reviewers with its error string and the panel continues; if all three
fail, exit 1. Build JudgeInput, run the judge, compute weighted_score and ship
with runtime/scoring.py. Leave a clearly named no-op `triage(diff) -> Decision`
hook point before the reviewers that #18 replaces. CLI: `factory review
<repo_path> --base <ref> --head <ref> [--json] [--reviewers a,b]
[--allow-unpublished]`. Exit 0 when ship is true, 1 when false or a hard error,
2 usage, 3 not found, 4 missing key. `--json` prints one document with
pipeline_run_id, per-reviewer status, judge output, weighted_score, ship,
total_cost_usd.

Tests (fake runner): all reviewers succeed; one fails and is reported; all fail
exits 1; --reviewers subset; JSON golden file. Live test on the mixed fixture
asserts four trace rows under one pipeline_run_id and that
cost_by_pipeline_run returns their sum.
```

#### #15 Jev SDK spike (Opus 5.5)

```text
Issue: #15. Branch: issue-15-jev-spike.
You own: docs/jev-sdk.md, examples/jev_hello.py.
Timebox: one working day. Stop at the end of the day regardless and write up
what you have.

Goal: answer, with evidence, whether Python can call TypeSafe Jev today well
enough to build Phase 4 on it.

Find out and record with a source link and the date for each: the Python
access path (vendor package, raw HTTP, OpenRouter, or nothing); the exact
current model id (`jev-1.13.0` and `typesafe/jev-1.14-20261014` both appear in
public sources; determine which is live and whether ids are pinned or rolling);
the wire format for Noul, Choice and Score questions, including what
confidence fields each returns (the plan assumes Noul returns only `p`);
batching (several questions in one request or not); input limits in characters
or tokens; price per request or per token; latency at roughly 60k characters of
input measured by you if a key is available, otherwise the published figure
marked as unverified. Secrets come from `agb sys secrets` if a Jev key exists
there; if there is no key, say so and do not block on obtaining one.

Deliverables: docs/jev-sdk.md with one section per item above plus a
"Go / no-go" section stating the decision and the reason in three sentences;
examples/jev_hello.py of about ten lines that asks one Noul question and prints
`p`, runnable with the key in the environment. If no-go: the doc states what
would change the decision, and the PR body proposes closing #16 to #20 as
won't-fix with triage implemented as a `kind: llm` agent on claude-haiku-5-5
returning a two-field structured output; open that issue with `gh issue
create` and reference it in the PR body.
```

#### #16 JevSpec (Sonnet 5.5)

```text
Issue: #16. Branch: issue-16-jevspec.
You own: registry/jev_spec.py, agents/diff-triage/agent.yaml,
tests/registry/test_jev_spec.py.
Precondition: #15 concluded go. Read docs/jev-sdk.md first; its wire format
decides the generated output models.

Goal: a validated `kind: jev` manifest and generated output models.

Complete JevSpec with: model (verified against docs/jev-sdk.md); input.render
(a template string rendered from the payload) and max_chars; questions, each
with id, type in noul/choice/score, text, and for choice an options list (2 to
10), for score a range and levels; gates, each naming a question id and a rule
(`p_above`, `p_below`, `confidence_below`, `answer_in`); routing, mapping
choice answers or gate names to reviewer lists; evals with min_pass_rate.
Validation: unique question ids, option counts, every gate and routing entry
references a real question, `confidence_below` is invalid on noul questions
(they have no confidence field), score range sane. Generate output models per
question type: noul `{p: float}`, choice `{answer: str, probs: dict[str,
float], confidence: float}`, score `{value: float, level: str, probs:
dict[str, float]}`; a `JevOutput` model with one field per question id.
Rewrite agents/diff-triage/agent.yaml to the corrected schema: a noul
"does this diff need human-quality review?" and a choice over the reviewer
sets, route-only, with placeholder thresholds and a comment that #20 sets them.

Tests: one per validation rule above, plus a round-trip of a valid manifest and
generation of the output model with the right fields.
```

#### #17 jev_runner.py (Sonnet 5.5)

```text
Issue: #17. Branch: issue-17-jev-runner.
You own: runtime/jev_runner.py, runtime/jev_transport.py,
tests/runtime/test_jev_runner.py, tests/live/test_jev_live.py, the `[jev]`
extra in pyproject.toml.
Read docs/jev-sdk.md and registry/jev_spec.py first.

Goal: `run_jev(spec, payload, trace) -> JevOutput` with every Jev-specific detail
behind one transport interface.

Render input.render with the payload; truncate to max_chars, append a marker
line, and set `truncated=True` in the result metadata. Send questions batched
in one request if the API supports it per docs/jev-sdk.md, otherwise in
parallel threads. Timeout 5 seconds per request; retry with exponential backoff
on 429 and 5xx up to three attempts; anything else raises a typed JevError.
Parse responses into the generated output model from the spec. Write one trace
row with kind `jev`, the full probability vectors in output_json, truncated flag
in decision_json, token or request counts and cost from runtime/pricing.py (add
the Jev price with its date). The transport is a Protocol with one method; the
HTTP implementation lives in runtime/jev_transport.py and is the only module
that imports the HTTP client or vendor package. Put that dependency in the
`[jev]` extra; importing jev_runner without the extra raises a clear error at
call time, not import time.

Tests with a fake transport: render and truncate, batched or parallel path,
backoff on 429 then success, timeout raises, parse into model, trace row
shape. One live test, skipped without the Jev key, asks one noul question on a
short diff and asserts `0 <= p <= 1`.
```

#### #18 gates and triage route-only (Sonnet 5.5)

```text
Issue: #18. Branch: issue-18-gates-triage.
You own: runtime/gates.py, the triage section of pipelines/review_panel.py,
the `factory triage` subcommand, tests/runtime/test_gates.py,
tests/pipelines/test_triage_wiring.py.

Goal: gate decisions as code, and diff-triage running before the panel in
route-only mode.

`Decision` is a frozen dataclass: skip: bool, escalate: bool, route:
list[str], reason: str. `decide(spec, jev_output, truncated) -> Decision`
evaluates the manifest's gates and routing; a truncated input always yields
skip=False with reason "truncated". `ROUTE_ONLY = True` as a module constant
with a comment pointing at #20; while true, the panel logs the Decision in
the pipeline trace row (full probability vector included), reports it in the
JSON result, and still runs every reviewer. Replace the no-op triage hook in
review_panel with a call to run_jev on diff-triage@^0.1 followed by decide();
a Jev failure escalates (reason "jev_error") and the panel continues. Add
`--no-triage`. `factory triage <repo_path> --base --head [--json]` runs only
triage and prints the Decision and probabilities.

Tests: every gate rule in decide(); truncated forces skip=False; route-only
wiring runs all reviewers while logging skip=True; Jev failure escalates;
--no-triage bypasses. Live acceptance in the PR body: docs-only fixture yields
route=[] and skip=True in the log, and all reviewers still ran.
```

#### #19 judge-gate and Jev grader (Sonnet 5.5)

```text
Issue: #19. Branch: issue-19-judge-gate.
You own: agents/judge-gate/, the judge-gate step in pipelines/review_panel.py,
evals/graders/jev.py, a section in docs/jev-sdk.md, tests for each.

Goal: skip the Opus judge when reviewers agree and Jev is confident, and use
Jev as a cheap eval grader.

judge-gate is `kind: jev`, version 0.1.0: a noul "is this review set clear-cut?"
and a choice over ship / request_changes / unclear, rendered from the three
reviews and verdicts. In review_panel, after reviewers and before the judge:
run judge-gate; if the gate decides high confidence (threshold in manifest),
compute the verdict in code from the choice answer and skip the judge,
recording a Decision with reason "judge_gate"; low confidence or `unclear`
escalates to the judge. A truncated render always escalates. The JSON result
says which path ran and what the judge would have cost (estimate from
pricing). Eval runner: `grader: jev` sends expected and actual to a noul
question "does the actual output satisfy the expected?" and passes above a
threshold; on Jev error fall back to llm_judge and mark the case as
`grader_fallback` in the output. Run both graders over every existing case
and record agreement rate and cost in docs/jev-sdk.md under "Grader
agreement" with the date.

Acceptance (live, in PR body): clean-refactor fixture skips the judge; mixed
fixture escalates; trace shows the saving.
```

#### #20 Calibration and shadow mode (Opus 5.5)

```text
Issue: #20. Branch: issue-20-calibration (and follow-up branches as needed).
You own: evals/calibration/, scripts/calibrate.py, scripts/shadow_report.py,
docs/jev-calibration.md, threshold values in agents/diff-triage/agent.yaml.
This ticket spans weeks. Work in stages and open one PR per stage.

Stage 1: assemble at least 100 historical diffs from repositories the owner
names (ask in the PR if none are listed in the issue), each labelled risky or
safe with a one-line reason and stored as base and head SHA plus repository
URL, never as copied source. Keep labels in evals/calibration/labels.yaml with
a `labelled_by` and date per row.

Stage 2: scripts/calibrate.py runs diff-triage over all labelled diffs, sweeps
the skip threshold, and reports recall on risky diffs, skip rate on safe
diffs, and cost per diff at each threshold. The manifest gains
`min_recall_risky: 0.98`; the script fails if no threshold reaches it and says
so. Write the chosen thresholds into the manifest with the date and dataset
size in a comment.

Stage 3: shadow mode. The panel keeps ROUTE_ONLY true; scripts/shadow_report.py
runs daily (document a cron or `agb` hook, do not implement scheduling here)
and reports, from the trace, how many diffs triage would have skipped and
whether any of those produced a major or blocker finding from the panel. Run
for 14 days. If any skipped diff had a blocker, raise the threshold and reset
the clock.

Stage 4: flip ROUTE_ONLY to false behind a manifest flag, keep a permanent 5%
random sample of skipped diffs going through the full panel, and write
docs/jev-calibration.md: dataset, method, chosen thresholds, shadow results,
the sampling rule, and how to re-run calibration when the model id changes.

Rule from PLAN.md that binds this ticket: Jev never has the last word on
anything consequential, and a truncated diff always forces review.
```

#### #21 Fixer runtime decision (Opus 5.5)

```text
Issue: #21. Branch: issue-21-fixer-runtime.
You own: docs/decisions/0001-fixer-runtime.md, prototypes/fixer/.
Timebox: two days including a half-day prototype.

Goal: decide how the fixer runs, record why, and prove it on one fixture.

Compare three options against the same criteria: (a) the existing Tool Runner
runtime from #5 plus a hand-built Docker sandbox with a write tool, (b) the
Claude Agent SDK (`claude-agent-sdk`) driving a sandboxed working directory,
(c) Managed Agents (beta header `managed-agents-2026-04-01`) with its hosted
container. Criteria, each scored with a sentence of evidence: isolation (can
it write outside the worktree or reach the network), cost per fix on the
off-by-one fixture, latency, how the action-guard hook (#23) attaches, how the
run is traced with cost into runtime/trace.py, operational burden on the CasaOS
host (#28), and vendor lock-in. Load the claude-api skill before reading the
Agent SDK and Managed Agents docs; do not rely on memory for either.

Prototype (half day, pick the option you expect to win): fix the off-by-one
fixture end to end, producing a patch file, without the agent touching
anything outside its worktree. If the prototype fails, that is evidence; write
it down and try the runner-up only if time remains.

Deliverable: docs/decisions/0001-fixer-runtime.md in the format Context,
Options considered, Decision, Consequences, with the criteria table and the
prototype result. The PR body states the decision in one line and lists what
#22 to #24 must build as a result. Keep the prototype under prototypes/ and
mark it clearly as not production code.
```

#### #22 test-writer@0.1 and fixer@0.1 (Sonnet 5.5)

```text
Issue: #22. Branch: issue-22-test-writer-fixer.
You own: agents/test-writer/, agents/fixer/, tools/sandbox.py (write and run
tools, sandbox-only), tests for each.
Read docs/decisions/0001-fixer-runtime.md first and build on the runtime it
chose.

Goal: two agents that change code, allowed to write only inside the sandbox.

Shared: a `fs.write_file` tool and a `shell.run` tool in tools/sandbox.py,
registered in TOOLS but refused by tool_defs unless the spec carries
`sandbox: true`; both resolve paths against the worktree root and reject
anything outside it, and shell.run takes an argv list with a fixed allow-list
of executables (pytest, python, uv, git for read-only subcommands). Output
model for both agents: files_changed (path, action, summary), commands_run
(argv, exit code, truncated output), summary, and for test-writer the test
file paths and the expectation that the new tests fail on head before the fix.
Limits in both manifests: max_tool_turns 25, timeout_s 600, max_output_tokens
8192, plus `limits.max_tokens_total` (enforced in #25; declare it now).
test-writer@0.1: given a Finding and the worktree, writes a failing test that
reproduces it and runs it to confirm it fails. fixer@0.1: given the Finding and
the failing test, changes production code only, runs the test to confirm it
passes, and runs the full suite. Both on claude-sonnet-5-5, effort xhigh.

Evals: off-by-one fixture (test-writer produces a test that fails on head;
fixer makes it pass) and untested-change fixture (test-writer adds coverage;
fixer has nothing to fix and says so). Graded deterministically by actually
running the tests in the sandbox; llm_judge only for the summaries.
```

#### #23 action-guard hook (Sonnet 5.5)

```text
Issue: #23. Branch: issue-23-action-guard.
You own: agents/action-guard/, runtime/guard.py, the `guard` field in LLMSpec,
tests/runtime/test_guard.py.
Read runtime/hooks.py (from #5) and docs/jev-sdk.md first.

Goal: a per-tool-call guardrail that runs before any non-read-only tool.

action-guard@0.1 exists in two forms chosen by configuration: `kind: jev` with
a noul "is this tool call safe inside the sandbox?" when Jev is go, otherwise
`kind: llm` on claude-haiku-5-5 with a two-field structured output
(allow: bool, reason: str), effort low, no tools. runtime/guard.py implements
the hook protocol from #5: before_tool checks whether the tool is read-only
(registry flag); if not, renders the call (tool name, arguments, worktree root,
a short task summary) and asks the guard agent. Deny returns a tool result
saying the call was refused and why, so the model can adjust; the third denial
in one run ends the run with a typed GuardError. Every guard decision is a
trace row with kind matching the guard agent and the parent run id in
decision_json. LLMSpec gains an optional `guard: action-guard@^0.1`; the
runner installs the hook when it is present. Add it to the test-writer and
fixer manifests.

Tests with a fake guard agent: read-only tools bypass the guard; a write
outside the worktree is refused and the tool result carries the reason; a
shell command that would reach the network (curl, pip install, git push) is
refused; three denials end the run; decisions are traced with the parent run
id.
```

#### #24 fix_loop pipeline (Opus 5.5)

```text
Issue: #24. Branch: issue-24-fix-loop.
You own: pipelines/fix_loop.py, pipelines/sandbox.py, the `factory fix`
subcommand, tests/pipelines/test_fix_loop.py, tests/live/test_fix_loop_live.py,
docs/fix-loop.md.
Read docs/decisions/0001-fixer-runtime.md and pipelines/review_panel.py first.

Goal: `factory fix <repo_path> --finding <json>` produces a patch and a summary
without pushing anything, inside a sandbox.

Flow, one pipeline_run_id throughout: create a throwaway git worktree from the
repository at the given head; start the sandbox per the decision record (no
network, CPU and memory limits, the worktree mounted read-write and nothing
else); run test-writer; run fixer; run the full test suite in the sandbox and
stop with exit 1 if it fails; run the review panel on the worktree diff; run
judge-gate and, if it escalates, the judge; if ship is true write
<out_dir>/<pipeline_run_id>.patch (git diff of the worktree) and
<out_dir>/<pipeline_run_id>.json (summary: finding, files changed, commands,
panel result, cost); always remove the worktree and sandbox, also on error. The
pipeline never pushes, never commits to the source repository, and refuses to
run if the source repository has uncommitted changes. Budget: stop with exit 1
and a clear message when total cost crosses `--budget-usd` (default from a
config value; #25 generalises this). JSON output and exit codes follow the CLI
convention.

Tests (fake runner and fake sandbox): happy path writes patch and summary;
failing suite stops before the panel; panel says no ship, no patch written;
cleanup on exception; refusal on dirty source repository; budget stop. Live
test on the off-by-one fixture asserts a patch that applies and makes the
planted test pass, and that all agent rows share one pipeline_run_id.
```

#### #25 Retries and cost guard (Sonnet 5.5)

```text
Issue: #25. Branch: issue-25-cost-guard.
You own: the limits handling in runtime/runner.py and runtime/jev_runner.py,
runtime/budget.py, the `factory cost` subcommand, tests for each.

Goal: bounded retries, bounded tokens, bounded dollars.

Manifests: `limits.max_retries` (API-level retries on 429, 529 and 5xx, with
backoff; distinct from the single validation retry) and
`limits.max_tokens_total` (sum of input and output across all turns of one run;
exceeding it raises a typed error with the partial transcript). Validate both
in LLMSpec with sane defaults. runtime/budget.py: a `Budget(usd)` object a
pipeline passes to every run(); each trace row's cost is charged against it and
the first run that would exceed it raises BudgetExceeded, which pipelines turn
into exit 1 with the spend so far in the message. Default pipeline budget from
a config value with a CLI override `--budget-usd`; #24 already has a local
version, replace it with this. `factory cost --since <date> [--by agent|
pipeline] [--json]` prints spend from the trace store using the aggregates
from #6, including eval and publish tags separately.

Tests: retry on 429 then success; max_retries exhausted raises; max_tokens_total
exceeded raises with transcript; budget stops the second run when the first
spent most of it; cost command output golden file.
```

#### #26 Async runtime (Opus 5.5)

```text
Issue: #26. Branch: issue-26-async.
You own: runtime/async_runner.py, the async path in runtime/trace.py,
pipelines/review_panel.py (reviewer fan-out), tests/runtime/test_async_runner.py.

Goal: run reviewers concurrently on AsyncAnthropic without losing the sync CLI.

Add `arun(spec, payload, trace)` on `AsyncAnthropic` and the async Tool Runner
with identical semantics to run(): same hooks, limits, validation retry, error
types and trace row. Share the request-building and response-parsing code with
the sync runner; do not fork the logic. The trace store gets a single-writer
path: either one writer task fed by an asyncio.Queue, or a lock around the
connection, chosen and documented after checking what DuckDB allows for
concurrent writers from one process. review_panel fans out reviewers with
asyncio.gather and wraps the whole pipeline in asyncio.run so the CLI stays
synchronous. Keep run() for callers that want it (evals, tests) as a thin
wrapper over arun. Measure wall time of the panel on the mixed fixture before
and after and put both numbers in the PR body.

Tests: arun happy path and tool call (mocked async client); gather of three
agents writes three trace rows with no lost rows under a stress test of 50
concurrent fake runs; hooks fire in the async path; sync wrapper still passes
the #5 tests unchanged.
```

#### #27 CI and test harness (Sonnet 5.5)

```text
Issue: #27. Branch: issue-27-ci.
You own: .github/workflows/ci.yml, .github/workflows/live.yml,
tests/golden/, tests/test_golden_cli.py, coverage settings in pyproject.toml.

Goal: every PR gets lint, types, tests with a coverage floor and the publish
gate; live tests run only on demand.

ci.yml on pull_request and push to main: uv setup with cache, `ruff check`,
`ruff format --check`, `ty check`, `pytest --cov --cov-fail-under=85` with
live tests deselected by marker, and the version-bump check from #10 against
the base branch. live.yml on workflow_dispatch only, with ANTHROPIC_API_KEY and
the Jev key from repository secrets, running the live marker. Golden JSON
tests: for every `factory` command that supports `--json` (list, review,
triage, eval, publish-check, cost, fix), run it with fake runners against a
fixture and compare to a checked-in golden file, with an `--update-golden`
pytest option. Record a small README in tests/golden/ on how to refresh them.

Acceptance: CI green on this PR; coverage at or above 85% (if the current
figure is below, raise it with tests on the lowest-covered modules, not by
excluding them, and say what you added); a manual run of live.yml succeeds
once and its link is in the PR body.
```

#### #28 Deployment (Sonnet 5.5)

```text
Issue: #28. Branch: issue-28-deploy.
You own: Dockerfile, .dockerignore, compose.yaml, docs/deploy.md,
.env.example, optional api/ with a FastAPI app behind the `[api]` extra.

Goal: a container that runs one factory command per invocation on the CasaOS
host, with persistent traces and no secrets in the image.

Multi-stage Dockerfile: uv installs into a virtual environment from uv.lock,
non-root user, `data/` as a volume mount point, entrypoint `factory`. Secrets
come only from an env file passed at run time; the image is scanned in CI (#27
workflow, add a job) for any string that looks like a key. compose.yaml defines
the service with the data volume, the env file, no ports unless the API
profile is enabled, and resource limits matching what #24's sandbox needs.
Optional FastAPI trigger behind a Compose profile: one endpoint that accepts
the same arguments as `factory review` and returns the JSON result; no auth
means it binds to localhost only, state that plainly. docs/deploy.md: build,
first run, where traces live, how to rotate the key, how to run one review from
the host, how CasaOS imports the Compose file, and how to upgrade.

Acceptance: `docker build` then `docker run --env-file .env ... factory
--version` works; a review of the mixed fixture from inside the container
writes to the mounted data volume; `docker history` and a grep of the image
layers show no key.
```

#### #29 Release hygiene (Haiku 5.5)

```text
Issue: #29. Branch: issue-29-release.
You own: CHANGELOG.md, the version source in pyproject.toml and
factory/__init__.py, docs/releasing.md.

Goal: a changelog, a version that comes from one place, and a first tag.

CHANGELOG.md in Keep a Changelog format with an Unreleased section and a
0.1.0 section that lists, under Added, one line per merged issue from #1 to
#28 (pull titles from `gh issue list --state closed`). `factory --version`
reads the version from package metadata (importlib.metadata), with no
duplicated literal. docs/releasing.md: bump version in pyproject.toml, move
Unreleased to a dated section, commit, `git tag -a vX.Y.Z`, push tag, create
the GitHub release from the changelog section with `gh release create`.

Do not tag in this PR. The PR body states whether the handover definition of
done (docs/HANDOVER-v2.md section 6) is met, item by item, with a link to the
evidence for each; the owner tags v0.1.0 after merging if every item is met.
```

#### #30 `agb factory` wrapper (Sonnet 5.5)

```text
Issue: #30 in agb-agent-factory; the code lives in the agb-tools repository.
Branch there: factory-wrapper.
You own, in agb-tools: the `factory` noun module, its tests, the `agb sys
doctor` section, and the `agb` skill entry for the new noun.
Precondition stated in the issue: the factory CLI has been in daily use. Confirm
with the owner that this holds before starting; if not, stop.

Goal: `agb factory review|triage|fix|cost` as thin pass-throughs to the
factory CLI, following agb-tools conventions exactly.

Read the agb-tools CONTRIBUTING or equivalent and two existing nouns before
writing anything. The wrapper locates the factory via a configured path or
`uv run --project <path> factory`, passes arguments through unchanged, keeps
the factory's exit codes, and adds nothing of its own except the `--json`
convention agb already uses if it differs. `agb sys doctor` gains a section
that reports whether the factory is installed, whether its data directory is
writable, whether ANTHROPIC_API_KEY is present, and the last trace timestamp.
Every verb's help ends with an example, as the agb convention requires. Update
the agb skill so it mentions the noun.

Acceptance: `agb factory --help` lists the four verbs with examples; the
doctor section appears; tests cover argument pass-through and exit code
preservation with a fake factory binary.
```
