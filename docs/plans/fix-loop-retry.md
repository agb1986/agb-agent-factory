# Plan: bounded retry in the fix loop

Status: proposed, 2026-10-28. Not started. Needs `parent_run_id` (trace migration 3), which is in.

## Why

`run_fix` makes one attempt. When the sandbox suite fails it raises `SuiteFailedError`, and when the
panel does not approve it writes a report and no patch. In both cases the fixer's work is lost
although the failure text is exactly what a second attempt needs. A retry is a loop, not a graph:
the number of passes is not known in advance and the loop body is the same each time. Do not model
it as a graph cycle.

## Behaviour

- `run_fix(..., max_attempts=1)`. The default keeps today's behaviour. `factory fix --attempts N`
  raises it, capped at 3 (a larger value is a `UsageError`).
- Attempt 1 is today's flow. The test-writer runs once; it is not repeated.
- A later attempt starts the fixer again in the same clone, from the last committed state, with the
  failure of the previous attempt added to its input:
  - suite failure: the tail of the suite output (`_tail`, already 400 characters for the hint; use
    a larger fixed limit for the model, in one named constant);
  - panel did not approve: the judge's blocking findings.
- The fixer input gains an optional `previous_attempt` field. That is a schema change, so it bumps
  the fixer's version and its evals have to pass again (see the versioning rule in PLAN.md).
- The loop stops at the first attempt whose suite passes and whose panel approves, at
  `max_attempts`, or when the budget is spent. The result and the report carry every attempt: its
  fixer `run_id`, the suite result and the panel decision.

## Trace shape

Each fixer attempt names the previous attempt's fixer as its `parent_run_id`; attempt 1 names the
test-writer, as now. The panel rows of an attempt name that attempt's fixer. `factory trace` then
draws the retries as a chain, and each panel as a branch off the attempt that produced it.

## What stays the same

- `TestFileGuard` and the post-check protect the tests on every attempt. A refusal ends the run
  with `TestFileWriteError` and is never retried: it is a rule break, not a failure to fix.
- `BudgetExceeded` ends the loop immediately and is never retried.
- The source repository is still never written to.

## Steps

1. Pull the body of one attempt out of `run_fix` into a function that returns an `Attempt`
   (fixer run id, commit, suite result, panel or None). No behaviour change; the existing tests pass.
2. Add the loop, `max_attempts`, the `--attempts` flag and the result and report fields. Fake
   runner tests: a first attempt that fails the suite and a second that passes; a panel that blocks
   once and then approves; the cap reached; the budget reached; a guard refusal that is not retried.
3. Add `previous_attempt` to the fixer's input, update `system.md`, bump the version, add eval cases
   for the retry input and publish through `factory publish-check`.
4. A live test next to `tests/live/test_fix_loop_live.py`, skipped without a key, on a fixture whose
   first fix is known to fail. Run it once by hand before merging; it spends money.
5. Document the flag in `docs/fix-loop.md` and the new trace edges in `docs/CONVENTIONS.md`.

## Open questions

- Should the suite output reach the fixer in full or as a tail? Start with a tail and look at the
  eval cases before changing it.
- Should a retry re-run triage on the new diff, or reuse the first one? Triage is cheap and the
  diff changed, so run it again; confirm that against the cost report after the first live run.

## Out of scope

- Retrying the test-writer.
- Running attempts in parallel.
- A general retry helper in `pipelines/stage.py`; add one only if a second pipeline needs it (see
  [shared-pipeline-parts.md](shared-pipeline-parts.md)).
