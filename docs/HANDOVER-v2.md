# Agent Warehouse — Handover Plan (v2, with Jev)

## 1. What this is

This project is a registry of reusable software-engineering agents. Each agent is a versioned package. Pipelines look up agents by name and semver constraint, then run them through a common `run(spec, payload)` interface.

There are two kinds of agent:

| Kind | Backend | Does | Use for |
|---|---|---|---|
| `llm` | Anthropic API (Claude) | Reasons, uses scoped tools, writes findings | Review, test writing, fixing, judging |
| `jev` | TypeSafe Jev | Returns probabilities over answers fixed in advance (Choice / Score / Noul). Generates no text and calls no tools. | Triage, routing, guardrails, confidence gates, cheap eval checks |

**The design principle:** Jev decides *whether* and *where* work is sent, and Claude does the work. Jev never has the last word on anything with real consequences. Low-confidence Jev answers escalate to Claude or the full panel.

**Status:** scaffold only. The files compile, but nothing has been run against either API. There are no tests. The Jev integration is designed but not implemented.

## 2. Architecture

```
            payload (repo, refs, diff)
                     │
             ┌───────▼────────┐
             │ diff-triage    │  kind: jev  (~130 ms, very cheap)
             │ needs_review?  │
             │ risk_area?     │
             │ effort?        │
             └───┬───────┬────┘
        skip ◄───┘       └───► route (risk_area → reviewers)
                                │  low confidence → full panel
                 ┌──────────────┼──────────────┐
          code-reviewer   security-reviewer  perf-reviewer   kind: llm, parallel
                 └──────────────┼──────────────┘
                         ┌──────▼──────┐
                         │ judge-gate  │  kind: jev, "is the merged result clear-cut?"
                         └──┬───────┬──┘
              high conf ◄───┘       └───► low conf → judge (Opus)  kind: llm
```

In the fix loop, a `jev` guardrail (`action-guard`) checks every write-tool call before it runs.

## 3. What exists

| Path | Purpose | State |
|---|---|---|
| `agents/code-reviewer/` | Manifest, prompt, schemas, 4 eval cases | Done; eval fixtures missing |
| `agents/judge/agent.yaml` | Opus judge, weighted scoring | No `system.md` / `schemas.py` |
| `agents/diff-triage/agent.yaml` | Jev triage manifest: questions, gates, routing | Manifest only; no runtime support |
| `registry/resolver.py` | `resolve(name, "^1.2")` returns `AgentSpec` | `llm` only; no `kind` branch |
| `runtime/runner.py` | Claude loop, scoped tools, `submit_result` structured output | Done, untested |
| `runtime/trace.py` | DuckDB run log, thread-safe, cost query | Done, untested |
| `pipelines/review_panel.py` | 3 parallel reviewers, then the judge | No triage; depends on missing agents |

### Conventions (don't break these)

- **Agent directories use hyphens.** The resolver loads `schemas.py` by file path, not by import.
- **LLM output goes only through `submit_result`.** Its `input_schema` is the output model. On the last turn the runner forces that tool. Validation errors are returned to the model.
- **Tool scoping is enforced in the runtime.** Dotted names become `__` on the wire.
- **Gates and routing run in code, never in the model.** Thresholds live in the manifest, and `runtime/gates.py` applies them to the Jev probabilities. The same rule applies to the judge's weighted score.
- **All Jev SDK calls stay inside `runtime/jev_runner.py`.** The SDK is early, and minor versions have broken the question types, so pin it and keep the blast radius to one file.
- **Agents are stateless.** All state lives in the payload and the trace store.

## 4. Work plan

Each task has acceptance criteria. Do the phases in order, because each one unblocks the next. Tasks marked **[J]** are the new Jev work.

### Phase 1 — Make the LLM path run (blocking)

1. **Packaging**
   - Add `pyproject.toml` with these dependencies: `anthropic`, `pydantic>=2`, `pyyaml`, `packaging`, `duckdb`, `pandas`.
   - Add `typesafe-sdk==<pinned>` as an optional extra: `[jev]`.
   - Add `__init__.py` to `registry/`, `runtime/`, `tools/` and `pipelines/`.
   - Done when `pip install -e ".[jev]"` works and the pipeline module imports.
2. **`tools/registry.py`**
   - Create `TOOLS: dict[str, tuple[dict, Callable]]`.
   - Implement `git.read_file`, `git.show_diff` and `rg.search`, validating `repo_path`.
   - Done when there are unit tests against a temporary git repo and path traversal is rejected.
3. **Judge agent**
   - Write `system.md` and `schemas.py`.
   - `JudgeOutput` has the fields: per-dimension scores, `weighted_score`, `ship`, `merged_findings`, `rationale`.
   - The weights are applied in code after the model returns.
   - Done when one real diff produces a valid `JudgeOutput`.
4. **Smoke test**
   - Run `review_panel` with only `code-reviewer`.
   - Done when the output is valid and a trace row is written.

### Phase 2 — Jev runtime **[J]**

5. **Spike the SDK (1 day, timeboxed).**
   - Install the pinned `typesafe-sdk` and record in `docs/jev-sdk.md`:
     - client construction and auth
     - the exact request shape for Noul / Choice / Score
     - how several questions are batched in one request
     - the response field names (probability, per-option probabilities, confidence)
     - how input size is counted, plus the limits and error types
   - Done when there's a working script that sends one request with all three question types against a real diff, and the documented response shape. Everything below depends on this. Until then, treat the SDK details as unverified.
6. **Manifest `kind` and resolver.**
   - Add `kind: llm | jev` to the manifest, defaulting to `llm`.
   - Split `AgentSpec` into `LLMSpec` and `JevSpec`.
   - A `JevSpec` carries `questions`, `input.render`, `gates` and `routing`.
   - The resolver validates the questions: unique ids, options/levels present for choice/score, and no more than the SDK's option limit (reported as 255 [unverified]).
   - It generates the output Pydantic model from the questions:
     - `noul` becomes `{p: float}`
     - `choice` becomes `{answer: str, probs: dict[str, float], confidence: float}`
     - `score` becomes `{value: float, level: str, probs: dict[str, float]}`
   - Done when the resolver tests load `diff-triage` and reject malformed manifests.
7. **`runtime/jev_runner.py`**
   - Render the payload fields listed in `input.render` into Jev's state, truncated to `max_chars` with an explicit marker.
   - Send **one request with all questions**, then map the SDK response into the generated output model.
   - Use the same `run(spec, payload, trace)` signature as the LLM runner. `runtime/__init__.py` dispatches on `kind`.
   - Configure the timeout (default 5 s) and retries for 429 and 5xx.
   - Done when there are unit tests with a mocked SDK client and one live integration test (skipped without `TYPESAFE_API_KEY`).
8. **`runtime/gates.py`**
   - Evaluate the manifest's `gates` and `routing` against the Jev output and return a `Decision(skip: bool, escalate: bool, route: list[str], reason: str)`.
   - This is pure code with no model call.
   - Done when there are table-driven unit tests covering every gate type and the unmapped/escalate fallbacks.
9. **Trace changes**
   - Add the columns `kind`, `cost_usd` and `decision_json`.
   - Jev runs log input tokens only. Cost is computed from a price table in `runtime/pricing.py`, so prices aren't hard-coded in the queries.
   - Done when `cost_by_agent()` shows both kinds side by side.

### Phase 3 — Wire Jev into pipelines **[J]**

10. **Triage gate in `review_panel`.**
    - Run `diff-triage` first. Skip the panel if `skip` is set. Otherwise run only the routed reviewers, or the full panel if `escalate` is set.
    - Add a `--no-triage` flag that bypasses it.
    - Every skip is logged with its probabilities so it can be audited.
    - Done when a docs-only diff is skipped, a security-flavoured diff routes to code- and security-reviewer, and an ambiguous diff gets the full panel.
11. **`judge-gate` (Jev) in front of the Opus judge.**
    - Ask Noul questions over the merged findings: "Are all reviewers in agreement?", "Is any blocker-severity issue present?"
    - If confidence is high and the case is clear-cut, compute the result in code from the panel outputs. Otherwise call the Opus judge.
    - Done when the eval set shows the gate never accepts a case the Opus judge would reverse, as measured in Phase 4.
12. **Jev as a cheap grader in the eval runner.**
    - Rule-style rubric checks can use `grader: jev`, for example "Does this finding reference a real line in the diff?"
    - Nuanced rubrics keep `grader: llm_judge`.

### Phase 4 — Quality gates and calibration

13. **Eval runner** (`evals/run.py <agent>`)
    - Handles rule assertions plus the `llm_judge` and `jev` graders.
    - Exits non-zero below the thresholds.
    - For `jev` agents it reports **recall on risky diffs**, the skip rate and the escalation rate, not just accuracy.
14. **Fixtures**
    - Use `fixtures/build.sh` to script small git repositories for these cases:
      - an off-by-one bug
      - a clean refactor
      - an untested change
      - mixed issues
    - For triage, also add docs-only, test-only, security (SQL injection) and performance (N+1) cases.
15. **Triage calibration [J]**
    - Run `diff-triage` and the full panel on 100+ historical diffs, using real commits from your own repos.
    - Plot `needs_review` p against "did the panel find a major+ issue". Set `skip_review_if` so that **no diff with a major+ finding is skipped**, then measure the resulting skip rate and cost saving.
    - Do the same for `escalate_if` confidence.
    - Done when the thresholds are committed with the calibration data in `evals/calibration/` and the triage evals meet `min_recall_risky: 0.98`.
16. **Shadow mode before enforcement [J]**
    - For the first two weeks, the triage gate logs its decision but the full panel always runs.
    - Flip to enforcing only once shadow-mode recall holds.
17. **Publish gate**
    - Changes under `agents/<name>/` require that agent's evals to pass.
    - Any change to the prompt, schema *or questions* requires a version bump.

### Phase 5 — Fill out the panel and the fix loop

18. **`security-reviewer`** (`^1.0`) and **`perf-reviewer`** (`^0.3`), both `llm` with read-only tools.
19. **Fix-loop pipeline:** triage → test-writer → fixer → review panel → judge-gate/judge.
    - The fixer writes only inside a Docker sandbox: `--network none`, CPU and memory limits, a throwaway worktree.
    - Stop when the tests pass or after N iterations.
20. **`action-guard` (Jev guardrail) [J]**
    - Before any write tool runs, ask: "Is this action outside the stated task scope?", "Does this touch files unrelated to the failing test?", "Is this destructive (delete, force, mass rewrite)?"
    - Block above the threshold, and on block, return a tool error to the fixer explaining why.
    - Implemented as a hook in `runner.py` around tool dispatch, enabled per agent in the manifest with `guard: action-guard@^0.1`.
    - Done when a fixture where the fixer tries to edit unrelated files is blocked, and normal fixes pass.

### Phase 6 — Operations

21. **Retries and cost guard.**
    - Set explicit `max_retries` on both clients.
    - Add a per-run `limits.max_tokens_total`.
    - Add a per-pipeline budget that aborts when exceeded.
22. **Async.** Move to `AsyncAnthropic` plus the async Jev client with `asyncio.gather` if the thread pool becomes a bottleneck.
23. **Deployment.**
    - Ship a Docker image on the CasaOS host with a `data/` volume.
    - Pass `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY` via an env file, never in the image.
    - Run as one-shot containers or behind a small FastAPI trigger.
    - If going through a gateway (OpenRouter, Cloudflare, Vercel) instead of TypeSafe direct, do it inside `jev_runner.py` only.

## 5. Known issues and risks

- **Untested end to end.** Expect API-shape bugs in the Claude loop when content blocks are replayed.
- **Jev SDK instability.** It's at early versions, and breaking changes to the question types have shipped in minor releases. Mitigations: pin the version, keep all calls in one adapter, and add a live contract test in CI.
- **Jev recall.** One independent test reported it catching 6 of 7 planted defects where a frontier model caught 7 of 7. That's why it gates and routes but never makes the final call. Calibration (task 15) and shadow mode (task 16) are mandatory before it's allowed to skip work.
- **Skipped diffs are invisible failures.** A wrongly skipped diff produces no findings, which looks just like a clean diff. Mitigations: log every skip with its probabilities, and periodically sample skipped diffs through the full panel (for example 5%) to keep measuring recall in production.
- **No rationale from Jev.** Gate decisions are explained by the logged probabilities and thresholds only. If a human needs a reason, escalate to Claude.
- **Independent questions.** One Jev question's answer can't depend on another's within a single call. Express dependencies as code in `gates.py`.
- **Truncation.** Both runners must mark truncated input and output explicitly. Jev seeing half a diff is a silent-recall risk.
- **The trace store** is single-process DuckDB. Move to Postgres if several processes write to it.
- **Model, temperature and threshold values** in the manifests are placeholders until the evals and calibration tune them.

## 6. Definition of done (v0.1)

- [x] `pip install -e ".[jev]"` works and the tests pass (verified 2026-10-30 in a fresh venv; the suite also needs the `dev` dependency group, `uv sync`, for the `api` extra and pytest)
- [x] `review_panel` runs triage → routed reviewers → judge-gate/judge end to end (live suite green on 2026-10-30, 17 passed with both keys and `rg` on PATH; first on 2026-10-27 with `tests/live/test_review_panel_live.py` and the fix-loop panel run in #59)
- [x] Triage thresholds are calibrated on 100+ real diffs, skip none with a major+ finding, and have run 2 weeks in shadow mode (calibrated 2026-10-27 on 126 labelled diffs, recall 1.000, #57, #58, #61, and again on 2026-10-30 for `diff-triage@0.3.0` in #77, recall 1.000, safe skip rate 0.183; `factory publish-check diff-triage@^0.3` passed on 2026-10-30 at b5040e5, 7 of 7, so the 14-day shadow clock started that day; the end-of-window report on 2026-11-20 covered 17 panel runs, 2 would skip, neither with a finding, exit 0 under `--fail-on major`, and route-only was turned off on 2026-11-21 in `diff-triage@0.4.0`, see [jev-calibration.md](jev-calibration.md#shadow-results))
- [x] Every agent (both kinds) has evals that meet their thresholds (all nine run live on 2026-10-30 against the shared trace DB: `code-reviewer`, `security-reviewer`, `perf-reviewer`, `judge` and `judge-gate` pass; `diff-triage@0.3.0` 7 of 7 against 0.9 since #77; `action-guard@0.1.1` 8 of 8, `test-writer@0.1.2` 5 of 5 and `fixer@0.1.1` 4 of 4 against 0.75 since the #75 fix, which stopped the guard refusing writes inside a worktree under `/tmp`; the reviewer evals need `rg` on PATH)
- [x] `cost_by_agent()` shows Jev and Claude costs, with the triage saving measured against `--no-triage` (`factory cost --by kind`; [jev-calibration.md](jev-calibration.md#measured-against---no-triage))
- [x] The fix loop runs in a sandbox with `action-guard` on and has fixed at least one fixture bug autonomously (`off-by-one-red`, live run on 2026-10-27 in #59: panel approved, judge-gate ship, 0.09 USD)
