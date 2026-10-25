# Plan for agb-agent-factory

Source: the "agb-tools -> factory" handover (v2, with Jev) in [HANDOVER-v2.md](HANDOVER-v2.md), reviewed on 2026-10-25 against the current Claude API and the Jev documentation. This file is the working copy; the issue tracker is the source of truth for progress. Model assignments, delivery waves and the per-issue agent prompts are in [EXECUTION.md](EXECUTION.md).

## Decisions taken on 2026-10-25

- **Separate repository.** The factory carries heavier dependencies, long-running processes, API keys and a sandbox. `agb-tools` stays a thin CLI; an `agb factory` wrapper can be added there later.
- **Anthropic SDK, not a hand-rolled loop.** Reviewers, judge and triage run on the Anthropic SDK Tool Runner (`client.beta.messages.tool_runner`) with scoped tools. Per-turn hooks give logging, approval gates and the action guard. The fixer may use the Claude Agent SDK or Managed Agents instead; that decision is its own ticket.
- **Structured outputs replace `submit_result`.** Forcing `tool_choice` on the last turn returns 400 on current models. The output schema goes in `output_config.format` and the SDK parses it.
- **No `temperature`.** Current models reject it. Use `output_config.effort` where cost or speed matters.
- **Jev is a cost optimisation on top of a working pipeline, not the foundation.** It runs route-only until calibration data exists, and skipping review needs two weeks of shadow mode plus a measured recall of at least 0.98 on risky diffs. A truncated diff always forces review.
- **Jev transport is isolated.** Everything that talks to Jev lives in `runtime/jev_runner.py`, so moving between raw HTTP, OpenRouter and a future Python SDK touches one file.

## Stack

Python 3.14 managed by uv. `anthropic`, `pydantic>=2`, `pyyaml`, `packaging`, `duckdb`. Optional extra `[jev]` for the Jev transport. `ruff` and `ty` for lint and types, `pytest` with a coverage floor, `pre-commit`.

## Layout

| Path | Purpose |
| --- | --- |
| `agents/<name>/` | One agent: `agent.yaml`, `system.md` (LLM only), `schemas.py` (LLM only), `evals/cases.yaml` |
| `registry/` | Manifest loading, semver resolution, `LLMSpec` / `JevSpec` |
| `runtime/` | `runner.py` (LLM), `jev_runner.py` (Jev), `gates.py`, `trace.py`, `pricing.py` |
| `tools/` | Registry of scoped tool implementations, read-only by default |
| `pipelines/` | `review_panel.py`, `fix_loop.py` |
| `evals/` | Eval runner, calibration data |
| `fixtures/` | Scripts that build small fixture repositories with planted defects |
| `docs/` | Conventions, plan, Jev spike notes, decisions |

## Conventions (fixed in phase 1, see `docs/CONVENTIONS.md` once written)

| Area | Convention |
| --- | --- |
| Agent address | `name@^major.minor`, hyphenated directory names |
| Agent kinds | `kind: llm` (reasons, scoped tools, structured output) and `kind: jev` (probabilities over fixed answers, no text, no tools) |
| Output | LLM agents return exactly their output model via structured outputs; validation errors go back to the model once, then the run fails |
| Tools | Allow-list per agent, enforced in the runtime; the deny list is also enforced; dotted names become `__` on the wire |
| Gates | Thresholds, routing and skip decisions live in `runtime/gates.py` and return a `Decision`, never in a model |
| Trace | Every run is one row with kind, tokens, cost in USD, decision JSON and timing |
| Versioning | Any change to prompt, schema or questions bumps the agent version; pipelines pin ranges |
| Secrets | `ANTHROPIC_API_KEY` and the Jev key from the environment or `agb sys secrets`, never in the repository or an image |

## Phases

| Phase | Milestone | Issues | Scope |
|---|---|---|---|
| 1 Scaffold | Phase 1 | #1 to #7 | Conventions, package, tools registry, resolver, Tool Runner runtime, trace with cost, first reviewer end to end |
| 2 Evals | Phase 2 | #8 to #10 | Eval runner, fixture repositories with planted defects, publish gate |
| 3 Review panel | Phase 3 | #11 to #14 | Judge, security and performance reviewers, parallel panel pipeline |
| 4 Jev gate | Phase 4 | #15 to #20 | Timeboxed spike, `JevSpec`, `jev_runner.py`, gates, triage route-only, judge gate, calibration and shadow mode |
| 5 Fix loop | Phase 5 | #21 to #24 | Fixer runtime decision, test-writer and fixer agents, action guard, sandboxed fix loop |
| 6 Harden | Phase 6 | #25 to #30 | Retries and cost guard, async, test harness and CI, deployment, release hygiene, `agb factory` wrapper |

Each milestone on GitHub lists its issues; the ranges above are the initial set and later issues are added to the milestone, not to this table.

## Definition of done for an agent

Its manifest validates, its output model round-trips through the API, it has at least four eval cases with fixtures, its eval pass rate meets the manifest threshold, its runs appear in the trace with cost, and its version has been bumped for the change.

## Rules

- Jev never has the last word on anything consequential. Low confidence escalates to Claude.
- A skipped review is logged with its probabilities, and a sample of skipped diffs still goes through the full panel.
- Tools are read-only unless the agent runs inside the sandbox.
- No agent holds state between runs.
- Model ids, thresholds and prices are configuration, verified against the live API before use, never assumed from memory.

## Open questions

- How Python reaches Jev today: raw HTTP, OpenRouter, or a vendor SDK. The phase 4 spike answers this and records it in `docs/jev-sdk.md`.
- Exact current Jev model id (`jev-1.13.0` and `typesafe/jev-1.14-20261014` both appear in public sources).
- Whether the fixer runs on the Claude Agent SDK, Managed Agents, or a hand-built Docker sandbox.
- Whether `httpx2` (used by `anthropic` 1.x) and `httpx` (used by `agb-tools`) can share an environment, which matters only if the two repositories are ever installed together.
  - Answered in #2: `anthropic` 2.3.0 pulls `httpx2` only (no `httpx`), so the two repositories need separate environments unless `agb-tools` moves to `httpx2`.
