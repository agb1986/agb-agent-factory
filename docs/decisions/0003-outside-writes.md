# ADR 0003: Outside writes

Status: accepted, 2026-11-20. Issue: #83.

This record sets the policy for tools that write outside the sandbox: pull requests, review comments, issues and notifications. It is written before the first such tool exists, so that #84, #95, #100, #87 and #109 build against one rule. The rules themselves are in [CONVENTIONS.md](../CONVENTIONS.md#outside-writes); this record gives the reasons and what was rejected. Every choice the issue left open is marked **Proposed** below, so the owner can accept or change it in review.

## Decision in one line

An outside write is a named tool in the `github` or `notify` group that a model is never given: pipeline code calls it through `runtime.outside.write` on behalf of an agent whose `tools.allow` lists it, behind a code guard of the action guard's shape, only against the repository the pipeline was given, with `--dry-run` at the pipeline level and one trace row per call.

## Context

Today every tool reads, or writes inside the sandbox behind the action guard ([ADR 0001](0001-fixer-runtime.md), #23). The rule in [PLAN.md](../PLAN.md#rules) is that tools are read-only unless the agent runs inside the sandbox. The lifecycle the factory is growing into needs writes that land somewhere a person reads them:

- #84 adds the first such tools on the `gh` CLI: open a pull request, and post a review on one.
- #100 posts the panel's merged findings on a pull request (`factory review --pr N --comment`).
- #95 opens or comments on an issue that carries an escalation, and reads the answer back.
- #87 opens a pull request when the panel ships a ticket's change (`factory build`).
- #109 opens a hotfix pull request and stops there; a person merges and deploys.

What the runtime offers today, and where it does not fit an outside write:

- `ToolSpec` in `tools/_common.py` has three flags. `writes=True` puts a tool in `SANDBOX_ONLY`, which only an agent with `sandbox: true` may list. `reaches_out=True` puts it in `SANDBOX_DENIED`, which such an agent may never list. A spec with both is an error at import. An outside write changes something and reaches the network, so neither flag describes it.
- `READ_ONLY` is every spec without `writes`. A tool with only `reaches_out` would land in it, and the action guard lets `READ_ONLY` calls through unjudged.
- A tool's `impl(repo_path, **arguments, max_bytes=...)` is called by `runtime.runner` for a model's tool call. It sees the work tree and nothing else: not the pipeline run id, not a dry-run flag, not the trace.
- `ToolHook.before_tool(call) -> ToolDecision` is the shape of every guard. The action guard (`runtime/guard.py`) asks an LLM agent whether a call is safe inside the sandbox, and writes one trace row per decision.
- `TraceRow.kind` is constrained to `llm` or `jev` by a `CHECK` in migration 1, so a new kind of row needs a migration that DuckDB makes awkward.
- The writes in #87, #100 and #95 are decided by the pipeline, after the panel or the escalation: a pull request is opened on `ship`, findings are posted once the judge has merged them. No model in those flows chooses to write.

## Options considered

### (a) Named tools, called by pipeline code on behalf of a listing agent

Each outside write is its own `ToolSpec` with a new flag, `outside_write=True`. The registry puts it in a new set, `OUTSIDE_WRITES`, keeps it out of `READ_ONLY`, and, because the flag requires `reaches_out`, keeps it in `SANDBOX_DENIED`. `tool_defs` validates the name like any other but leaves it out of the definitions sent to the model. A pipeline calls the tool through one function, `runtime.outside.write`, naming the agent the write is made for; the function refuses unless that agent's resolved manifest lists the tool, passes the call through a code guard that implements `ToolHook`, and writes one trace row.

### (b) One generic `github.write` tool

One tool takes an operation name and its arguments, and one manifest entry allows every write to GitHub.

### (c) Writes performed by pipeline code without a tool

The build loop, the review command and the escalation path each call `gh` themselves.

### (d) A per-run allow flag instead of a manifest entry

A command-line flag such as `--allow-write github.create_pr` grants a write for one run, as `--allow-domain` replaces the web allow-list for one run.

### (e) Tools the model calls directly

The agent that holds the tool decides when to write, as it decides when to read a file.

## Decision

Use option (a).

- **A name per write.** The manifest names exactly which writes an agent's output may cause, and a review of the manifest shows it. The existing checks apply unchanged: an unknown name is an error when a run starts, a name in both lists is an error at load time, and an absent name is denied. Denied by default is the existing meaning of `tools.allow`.
- **Pipeline code calls it, the model never sees it.** Every write the next release needs is decided by a pipeline after a gate, so a model has no reason to hold one. Leaving the tool out of the wire definitions means a model cannot call it even if a prompt asks it to. The rule in PLAN.md, that a tool a model holds is read-only unless the agent is sandboxed, stays true without an exception.
- **A guard of the action guard's shape, in code.** The guard is a `ToolHook` whose `before_tool` returns `ToolDecision.allow()` or `ToolDecision.deny(reason)`, so the pipeline code that calls it and the tests that drive it look like those of the action guard. **Decided:** its checks are code, not an LLM. Whether a write is allowed is a question of which repository, which agent and how many times, each of which has one right answer, and the content was already judged by the panel or the escalation. PLAN.md keeps gates out of models.
- **One target.** A tool handed another repository refuses, and so does the guard before it, so a bug in either one alone cannot write elsewhere.
- **`--dry-run` everywhere.** The owner can see the exact request a pipeline would send, through the same code path, before the pipeline ever writes.
- **The trace is the audit log.** One row per attempted write, with the target and the outcome, under the pipeline run id, so `factory trace` shows what the factory did outside and why.

### Which agent lists each tool

**Decided:** the write is made on behalf of the agent whose decision it carries out. `github.create_pr` and `github.comment_pr` are listed by `judge`, whose `ship` verdict opens a pull request and whose merged findings a review comment carries. The coders cannot list them: `fixer` and `feature-coder` run with `sandbox: true`, and a sandboxed manifest may never list a tool that reaches out. When the judge gate skips the judge, the check still reads the resolved `judge` manifest, because the manifest is the authority, not the run. `notify.issue` is listed by `escalation-summary` (#94), whose summary the issue carries.

### Other decisions

- **Decided: the target comes from `origin`.** The target is the `owner/name` of the `origin` remote of the repository the pipeline was given, read once when the pipeline starts. There is no flag to name another, so no command line can widen it.
- **Decided: one write per tool and target in one pipeline run.** A second call to the same tool on the same target in the same run is refused. The build loop opens one pull request, the review posts one review, the escalation one issue; a loop that tries again is a bug. Idempotence across runs is the tool's job: `github.comment_pr` and `notify.issue` edit their earlier post, found by the marker below.
- **Decided: one marker format.** Every body an outside write posts ends with the line `<!-- factory:<tool> <key> pipeline_run_id=<id> -->`, where `key` is `pr-<N>`, `issue-<N>`, `ticket-<id>` or `branch-<name>`. A rerun finds its earlier post by the prefix `<!-- factory:<tool> <key> `.
- **Decided: pull requests come from `factory/` branches.** `github.create_pr` pushes a new branch named `factory/<pipeline_run_id>` to the target and opens the pull request from it to the default branch. It never pushes to an existing branch, never force-pushes and never pushes to a fork.
- **Decided: reads run in a dry run.** Read-only tools such as `github.read_pr` still run under `--dry-run`, so the plan shows the edit-or-post choice the real run would make.
- **Decided: a refusal ends the stage.** A refused write raises `OutsideWriteRefused`, a `FactoryError` (exit 1). There is no model to send a denial to, and a pipeline that wanted to write and may not needs a person.

## Rejected

- **(b) One generic `github.write` tool.** One entry would grant every write, so a manifest could no longer say "comment, never open a pull request", and adding a new operation would silently widen every agent that holds the tool. The guard would have to parse an operation name to know what it is judging, and the trace would need a second field to say what happened.
- **(c) Writes in pipeline code without a tool.** Each pipeline would grow its own `gh` calls, its own dry run and its own logging, and nothing in a manifest would show that a write can happen. The guard, the target check and the trace row would be rebuilt three times and kept in step by hand.
- **(d) A per-run allow flag.** It would let a command line grant what no manifest lists, so what an agent's output can cause would depend on how the command was typed rather than on a reviewed file. A flag may ask for a write the manifest already allows (`--comment`) or turn writes off (`--dry-run`); it never grants one. `--allow-domain` is different: it only replaces a list the manifest must already have, and the web tools only read.
- **(e) Tools the model calls.** No flow in the next release needs a model to decide to write, and giving a model a write would put the decision to publish in a model. A later record can allow it for a named tool and agent; the guard, the target check and the trace row would apply unchanged.
- **The LLM action guard for outside writes.** It answers whether a call is safe inside the sandbox; an outside write is not inside the sandbox, and its checks have exact answers. Running it would add cost and a source of variance without catching anything the code checks miss.

## Permitted tools for the next release

| Tool | What it writes | Listed by | Pipeline that may call it |
| --- | --- | --- | --- |
| `github.create_pr` | Pushes `factory/<pipeline_run_id>` and opens a pull request from it, with labels that already exist on the target (`hotfix` for #109) | `judge` | `factory build` (#87), `factory hotfix` (#109) |
| `github.comment_pr` | One review on a pull request, with inline comments by path and head-revision line, or one summary comment; a rerun edits the earlier one | `judge` | `factory review --pr N --comment` (#100) |
| `notify.issue` | Opens an issue with the escalation and its summary, or comments on the issue already open for that ticket | `escalation-summary` | The escalation path of #93 and #95 |

**Decided:** the group is `github`, not `gh` as #84 writes it, and the module is `tools/github.py`. The group names the service written to, as `sentry` does, rather than the program used to reach it, and `gh.` and `git.` differ by one letter in a manifest. `github.read_issue` and `github.read_pr` from #84 are read-only tools in the same module, with `reaches_out=True` only; they are not outside writes and a model may hold them.

Nothing else writes outside the sandbox in the next release. In particular no tool merges, closes, deletes, labels after the fact, releases, tags or deploys.

## Deployment

Deployment is out of scope and stays out until a separate decision record says otherwise. #109 writes that record as `docs/decisions/0004-no-automated-deploy.md`, which keeps it out until the risk gate has a measured recall on real incidents. Until a record allows it, no `deploy.` tool exists and a pipeline stops at a pull request.

## Consequences

### What the implementing issues must build

- **#84, GitHub tools.** Add `outside_write: bool = False` to `ToolSpec`, require `reaches_out` with it and forbid `writes` with it; build `OUTSIDE_WRITES` in `tools/registry.py` and leave it out of `READ_ONLY`; have `tool_defs` leave outside writes out of the definitions it returns, and have the runner count only the definitions it sends when it decides whether `repo_path` is required. Add `runtime/outside.py` with `OutsideContext` (target, `pipeline_run_id`, `dry_run`, the trace, the parent run id), the guard, `write(...)` and `OutsideWriteRefused`. An outside write's `impl` takes the context in place of `repo_path` and is called only by `write`. Add `tools/github.py` with `github.read_issue`, `github.read_pr`, `github.create_pr` and `github.comment_pr`, the marker, the trace row of [CONVENTIONS.md](../CONVENTIONS.md#outside-writes), and the tests for a refused repository, a dry run and an idempotent rerun. Record `GH_TOKEN` under Secrets in CONVENTIONS.md. Add `github.create_pr` and `github.comment_pr` to `judge`'s `tools.allow` with a version bump.
- **#100, `factory review --pr N --comment`.** Call `github.comment_pr` through `runtime.outside.write` on behalf of `judge`; `--comment` asks for the write and `--dry-run` turns it into a plan; the JSON document's `comment` section is the write's `output_json` and outcome.
- **#95, `notify.issue`.** Add `tools/notify.py`; list the tool in `escalation-summary`'s manifest; use the `ticket-<id>` marker key, and store the issue number on the ticket.
- **#87, build loop.** Resolve the target from `origin` at the start; pass one `OutsideContext` to `github.create_pr` on `ship`, on behalf of `judge`; `factory build --dry-run` writes the report with the planned pull request in place of its URL. Whether a dry run also moves the ticket on the board is a work-item question, and #87 decides it.
- **#109, hotfix pipeline.** Call `github.create_pr` with the label `hotfix`; never deploy; 0004 is the record that decides when that changes.

### What the decision rules out

- A model calling any outside write in the next release.
- A write to any repository but the pipeline's own, a push to anything but a new `factory/` branch, and a force-push.
- A command-line flag that grants a write.
- Merging, closing, deleting, releasing and deploying.
