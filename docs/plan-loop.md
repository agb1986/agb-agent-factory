# The plan loop

`factory plan` takes an idea to tickets in the work-item store, behind a human approval (#92). It composes two agents and the store: `product-architect` writes a PRD, a person reads it, answers its open questions and approves it, and `story-breakdown` cuts the approved PRD into tickets. The code is `pipelines/plan_loop.py`. The store is described in [work-items.md](work-items.md) and the rules it follows are in [ADR 0002](decisions/0002-work-items.md).

The plan loop never makes a ticket `ready`. Every ticket enters in `todo`, and only `factory ticket ready` (`WorkItems.ready()`) promotes the ones with no open dependency. No model call decides what runs next.

## The two steps

```console
$ factory plan ./repo --idea "Quote shipping by weight"
PRD: data/plans/<run-id>.md
Plan run: <run-id>
Not approved: 2 open question(s) have no answer.
Answer with --answer "<question or number>=<answer>" (or write after `A:` in the file), then run: factory plan <repo> --prd @data/plans/<run-id>.md --approve

$ factory plan ./repo --prd @data/plans/<run-id>.md --approve \
    --answer "1=euros" --answer "Is a = b allowed in a name?=yes"
Filed 2 ticket(s) in todo, parent prd:<run-id>:
  wi-1 [S] Rates
  wi-2 [S] Rounding (after wi-1)
  wi-2 runs after wi-1: both touch shop/rates.py
Nothing is ready: `factory ticket ready` promotes the roots.

$ factory ticket ready
```

Step one, `--idea TEXT` or `--idea @FILE`, runs product-architect on the repository at `--ref` (default `HEAD`). The PRD is printed and written to `data/plans/<run-id>.md`, and the command stops. Nothing else happens until a person approves.

Step two, `--prd @FILE --approve`, reads that file back, records the answers it was given, and runs story-breakdown on the PRD. The tickets are written to the store in one transaction. `--approve` may also be given with `--idea`, which writes the PRD and goes straight on if it has no open question; a PRD with a question stops at the questions, because a person has not yet answered them.

`--prd` without `--approve` only records answers. It makes no model call and needs no API key, so a person can answer in several calls and then approve.

One pipeline run id covers both steps: the second invocation reuses the id in the PRD's front matter, so `Trace.cost_by_pipeline_run` gives the plan's whole cost, and the work-item events carry it as `pipeline_run_id` with actor `pipeline:plan_loop`. The run id is not linked to each ticket with `link_run`, because that would count the plan's cost once per ticket. The link is the ticket's `parent`, `prd:<run-id>`.

## The PRD file

The file is the record of the plan and the way to hand it from one invocation to the next. It starts with front matter whose values are JSON strings: `plan_run_id`, `repository`, `ref` (the commit the idea's ref resolved to, so the second step plans against the same code), `idea`, `status` and `tickets`. Then come the PRD's sections: Problem, Goals, Non-goals, User stories, Constraints, Risks, Related files and Open questions.

`status` is `draft` until the tickets are filed and `filed` after, when `tickets` lists their ids. The command refuses a PRD that is already filed, whether its file says so or the store holds items whose parent is `prd:<run-id>`, so a stale copy of a draft cannot file the tickets twice. It refuses before any model call, with exit 1.

Only the Open questions section is read back. Each question is a list item `- Q<n>: <question>`, then `  Why: <why it matters>` and `  A: <answer>`. A person can write an answer after `A:` in an editor instead of using `--answer`, and an answer may continue on indented lines. The PRD text without the front matter, with the answers, is what story-breakdown receives, so it sees the decisions.

The PRD directory is `--plans-dir` (default `data/plans`, which is ignored by git). When a PRD is passed with `--prd`, the stored copy at `<plans-dir>/<run-id>.md` is the one rewritten.

## Open questions and `--answer`

A PRD with an open question that has no answer cannot be approved. `--approve` then exits 1, story-breakdown is not called, and the report says what is missing. These choices are the plan loop's own:

- **Matching.** `--answer "<question>=<answer>"` names a question by its text or by its number, `1` or `Q1`. The text is compared with whitespace collapsed and case ignored. The value is split at an `=`, trying each one left to right, so a question or an answer may contain `=`: `"Is a = b allowed?=yes"` is read as the question `Is a = b allowed?`.
- **Unknown question.** An answer that names no question is a usage error, exit 2, and nothing is changed. A typo must not look like an answer. The same holds for an empty answer, for two answers to one question in one command, and for any answer when the PRD has no questions.
- **Recording.** An answer is written into the stored PRD after `A:`, and a later invocation may replace it, so the file always shows the answer the plan was approved with.
- **Needs `--prd`.** `--answer` works on a stored PRD. With `--idea` there is nothing to answer yet, so the combination is a usage error.

story-breakdown can also return `open_questions` of its own. They do not block the plan: the tickets are filed in `todo`, the questions are printed on stderr, listed as `breakdown_open_questions` in the JSON document and appended to the PRD file under "Breakdown assumptions". A person reads them before running `factory ticket ready`, and the tickets can be changed with `factory ticket`. The reason is that the PRD, not the breakdown, is what a person approved, and a question the breakdown could not answer from the PRD is a note on the tickets, not a reason to lose a plan that is otherwise complete.

## Tickets and dependencies

story-breakdown returns tickets with dependencies as indexes into its own list. The plan loop turns each ticket into a `Ticket` with `PlannedTicket.as_ticket()` and writes the whole batch with `WorkItems.add_many`, which maps the indexes to the new `wi-<n>` ids, checks every dependency and runs the acyclic check over the whole graph. The writes are all or nothing: if any ticket is refused, no ticket is stored and the PRD stays a draft. The store write comes first and the file is updated to `filed` after it, so a failure between the two leaves tickets in the store with a draft file, which the store check above then refuses to file again.

Two tickets that are not ordered by dependencies and share an impacted path would edit the same file in parallel. `StoryOutput.parallel_conflicts()` lists them. The plan loop does not refuse the plan. For a pair that shares a path, the later ticket gets a dependency on the earlier one, unless it already follows it through others, and the pair is reported as `serialized`. The added edge always points from the later ticket to an earlier one, so it cannot close a cycle. A pair where a ticket has no impacted paths is reported as `unknown_paths` and left parallel, because an empty list means "not known", not "the same file".

Each ticket is written with kind, repository and ref from story-breakdown's output. The repository is `--repository` (default the absolute path of the repo, as a ticket without a remote uses) and the ref is the commit the PRD was pinned to.

## Exit codes and the JSON document

| Exit | When |
| --- | --- |
| 0 | A PRD was stored and is waiting for answers or approval, answers were recorded, or the tickets were filed |
| 1 | `--approve` was given and the plan did not reach `filed` (questions without answers); the PRD is already filed; the run failed or was over budget |
| 2 | Usage: neither or both of `--idea` and `--prd`, `--answer` with `--idea` or without questions, an unknown or empty answer, `--prd` without `@`, `--ref` or `--repository` with `--prd` |
| 3 | The repository or the `--prd` file does not exist, or an agent is not found |
| 4 | `ANTHROPIC_API_KEY` is not set, checked only when the invocation needs a model |

Under `--json`, stdout carries one document and the text goes to stderr. The document has `pipeline_run_id`, `status` (`awaiting_answers`, `awaiting_approval` or `filed`), `approved`, `prd_path`, `repository`, `ref`, `open_questions` (`question`, `why_it_matters`, `answer`), `unanswered`, `summary`, `tickets` (`id`, `title`, `size`, `status`, `depends_on`, `impacted_paths`), `conflicts` (`earlier`, `later`, `paths`, `resolution`), `breakdown_open_questions`, `cost_usd` and `duration_ms`. A negative result still prints its document before it exits 1. `tests/golden/plan.json` pins it.

## Options

`factory plan <repo> (--idea TEXT|@FILE | --prd @FILE) [--answer Q=A ...] [--approve] [--ref REF] [--repository NAME] [--plans-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]`. Both agents (`product-architect@^0.1`, `story-breakdown@^0.1`) are resolved before the first request, published versions only unless `--allow-unpublished`, so a missing one exits 3 before any model is called. The run is charged to one `Budget`.

## Testing

`tests/pipelines/test_plan_loop.py` runs the loop with a fake runner and a DuckDB file in a temporary directory, with no key and no network. `tests/live/test_plan_loop_live.py` plans a feature on the `storefront` fixture against the real API; it is behind the `live` marker and has not been run as part of #92.
