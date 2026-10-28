# Plan: shared pipeline parts, then a pipeline spec if it is still needed

Status: proposed, 2026-10-28. Not started. Builds on `parent_run_id` (trace migration 3).

## Why

`pipelines/review_panel.py` (fan-out, then a judge) and `pipelines/fix_loop.py` (test-writer, fixer,
suite, then the review panel) are the only two multi-stage pipelines, and `fix_loop` already calls
`run_panel` by hand. A third pipeline would copy the same wiring a third time. The cost of waiting
is small, so this plan does not start until a third pipeline is planned, or until a change to the
shared wiring has to be made twice.

A declarative graph (nodes and `needs` edges, run with `graphlib.TopologicalSorter`) is the last
step, not the first. Extracting plain functions first shows whether any wiring is left for a spec
to remove.

## Step 0: confirm the duplication

Read both pipelines end to end and list what they repeat. Seen so far, from the top of the files
only:

- `run_id = pipeline_run_id or new_pipeline_run_id()` and `budget = budget or Budget(DEFAULT_BUDGET_USD)`.
- Every runner call passes `pipeline_run_id`, `budget` and, since migration 3, `parent_run_id`
  (`review_panel._lineage` builds that keyword only when there is a parent, so runners that take
  fixed keywords keep working).
- A decision row written after each Jev step (`triage:decision`, `judge_gate:decision`).
- `_LockedTrace` for runs that share a trace across an event loop.

Stop here if the list is short. Record the finding in the pull request and close the plan.

## Step 1: a `StageContext` and a `stage()` helper

Put in `pipelines/stage.py`:

- `StageContext(pipeline_run_id, trace, budget, repo_path)`, built once per pipeline run.
- `stage(ctx, runner, spec, payload, *, after=None, **extra) -> StageResult`, which makes the
  `run_id`, passes `pipeline_run_id`, `budget`, `parent_run_id=after` and `run_id`, and returns the
  output together with the `run_id`, so the next stage can name it.

`fix_loop` and `review_panel` switch to it with no change in behaviour. The existing tests are the
check; the only new test covers the keyword rule (no `parent_run_id` keyword when `after` is None).

## Step 2: a `fan_out()` helper

`review_panel` gathers its reviewers under `asyncio.gather(return_exceptions=True)` and then raises
the first error in report order after every reviewer's row is written. Move that rule into
`fan_out(ctx, specs, make_call)` so the next fan-out (one fix per finding, one reviewer per file
group) does not re-derive it. Keep `BudgetExceeded` propagating as it does now.

## Step 3: decide on a spec

Only now ask whether a declarative form removes anything that is left. Write the graph for the
pipeline that triggered this plan on paper first. Build the spec layer if all of these hold:

- the new pipeline needs at least two parallel branches that join, or a node that is itself a
  pipeline;
- the wiring in code is longer than the graph that describes it;
- the edges are static. Conditional edges (Jev `skip` and `route`, once stage 4 of #20 lands) stay
  in code. A graph with conditions in it is a worse way to write an `if`.

If it is built: `pipelines/graph.py`, nodes as small frozen dataclasses (`name`, `run`, `needs`),
`graphlib.TopologicalSorter` for order and cycle errors at load time, layers run with `fan_out`,
and each node's `after` set from its first `needs` entry. The trace stays a tree: a node that joins
several branches names one parent, and the fan-in is not recorded. If fan-in needs to show, add a
`trace_edges(run_id, parent_run_id)` table in a new migration rather than a second column.

Do not add `networkx` or `langgraph`.

## Out of scope

- Retry loops (see [fix-loop-retry.md](fix-loop-retry.md)); they stay ordinary loops.
- Acting on triage decisions (#20, stage 4).
- Changing any agent's prompt or schema, which would need a version bump.

## Done when

- Both pipelines use the shared helpers and their tests pass unchanged apart from the new ones.
- `docs/CONVENTIONS.md` names `pipelines/stage.py` in the layout table.
- If Step 3 built a spec, a decision record under `docs/decisions/` explains why.
