# The hotfix pipeline

`factory hotfix <id>` takes one reproduced incident ticket to a reviewed pull request labelled `hotfix`, and stops there (#109). The ticket comes from `factory incident` ([incident.md](incident.md)), whose body carries a test that fails the way the production event did. That test is the finding: the fixer changes the code until it passes, the whole suite runs in the sandbox, the review panel reviews the change, and when the panel ships, `github.create_pr` opens the pull request on behalf of the judge. A person merges it and deploys. The factory has no merge call and no deploy tool, and [ADR 0004](decisions/0004-no-automated-deploy.md) says on what evidence that may change. The code is `pipelines/hotfix.py`; it composes `fixer@^0.1` (team `dev`) and the review panel, through the same stage `factory fix` runs (`pipelines.fix_loop.fix_and_review`, [fix-loop.md](fix-loop.md)).

## Usage

```sh
factory hotfix wi-4 --repo ./widgets --dry-run --json
factory hotfix wi-4 --repo ./widgets
```

| Option | Meaning |
| --- | --- |
| `--repo PATH` | The repository to fix; default the ticket's `repo` when it is a path, else the working directory. It gains objects but no branch or tag |
| `--dry-run` | Run every agent and every read, open no pull request and move no work item |
| `--out-dir DIR` | Where the report goes, default `data/hotfixes` |
| `--work-dir DIR` | Where the clone is made, default the system temporary directory |
| `--budget-usd N` | Cap the spend of the whole run, default `DEFAULT_BUDGET_USD` |
| `--allow-unpublished` | Resolve unpublished versions of the agents too |
| `--json` | Print the report as one JSON document on stdout and the text on stderr |

### Operator preconditions

- `ANTHROPIC_API_KEY` and `GH_TOKEN` are exported, a dry run included, since the dry run reads the repository's labels.
- Docker is reachable and the sandbox image is present ([fix-loop.md](fix-loop.md#sandbox-image)).
- The `hotfix` label exists on the target repository. `github.create_pr` checks the labels before it pushes, in a dry run too, and refuses a missing one with `OutsideWriteRefused` (exit 1). The factory never creates a label: create it on GitHub once, by hand.
- The repository is a clean clone whose `origin` is the ticket's repository on github.com (ADR 0003), and the ticket's `ref`, the release the event came from, resolves in it.

## What it takes

A work item of kind `incident` in `ready`, whose body has the layout `factory incident` writes and says `- Reproduced: yes`. Anything else is refused with exit 2 before anything runs:

- another kind, with a pointer to `factory build`;
- another status, with a pointer to `factory ticket ready`;
- a body `incident_sections` rejects;
- an incident that did not reproduce. `factory ticket ready` promotes it like any `todo` item without dependencies, so `ready` alone does not mean reproduced for an incident; the hint sends it to a person or back to `factory incident`;
- a reproduced body with no test code, no exception or crash file, an unreadable Culprit section, or a test at an absolute path, under `..` or under `.git`.

## Flow

Every agent call, the outside write and the store events carry one `pipeline_run_id`, are charged to one `Budget`, and have the actor `pipeline:hotfix`.

1. Preflight, as `factory build` does it, with the work item untouched: the item, the repository, its `origin`, `--work-dir`, `--out-dir` and the sandbox.
2. The pipeline run is linked to the item and, unless it is a dry run, the item moves `ready` to `in_progress`.
3. The ticket's `ref` is cloned (`pipelines.sandbox.checkout`). The reproduction test files from the body are written into the clone and committed on their own.
4. The test runs in the sandbox, as `factory incident` ran it. It must still fail with the event's exception raised from the crash file (`evals.graders.incident.reproduces`), or the run stops with `HotfixError` before any model is called.
5. `fix_and_review` runs the fixer, with the finding built from the ticket (`hotfix_finding`: the culprit's file and lines, the stack trace, the test and the rule that the code changes, not the test) and the test files protected; the fixer's work is committed, the fixer must have changed something, the whole suite runs in the sandbox, and the panel reviews the diff from `ref` to the fixer's commit, the reproduction test included. Triage is logged and never acted on here, so every reviewer runs.
6. There is one round. When the panel asks for changes the run stops with `HotfixChangesRequestedError`, which escalates: a person reads the findings in the record and answers it, or fixes the item by hand.
7. When the panel ships, the fixer's commit is fetched into the source repository's object store, with no ref, and `github.create_pr` pushes `factory/<pipeline_run_id>` and opens the pull request with the `hotfix` label, on behalf of the judge and with the judge's trace row as the parent (ADR 0003). The item moves to `in_review`.
8. Every other end after preflight goes through one function, `_stop_item`. A stop a person can answer (a request for changes, the budget, a write to the protected test, a red suite, a fixer that changed nothing, a test that no longer reproduces) writes an escalation record through `runtime.escalations.escalate` and moves the item to `escalated`; every other stop, a crash, and a stop whose record cannot be written moves it to `blocked` ([CONVENTIONS.md](CONVENTIONS.md#triggers-and-the-build-loops-stop-table) has the table). Right after a record is written, `_stop_item` calls `runtime.after_escalation.after_escalation`, as the build loop does, so the escalation-summary agent writes a summary of the record under the run's budget; a budget stop and a dry run get none. Then the record, with the summary when one was stored, is posted to the ticket's GitHub issue with `notify.issue`, except in a dry run. Neither step changes the item's state or the exit code ([CONVENTIONS.md](CONVENTIONS.md#summaries), [Posting to GitHub](CONVENTIONS.md#posting-to-github)). The reason is `<ErrorClass>: <message>; report <path>` either way. The clone and any container over it are removed however the run ended, and the report is written last.

A dry run does all of this except the push, the pull request and the item moves.

## Outputs

The report is `<pipeline_run_id>.json` in `--out-dir`, the same document `--json` prints: `pipeline_run_id`, `work_item` (`id`, `status_before`, `status_after`, `ticket`), `repo_path`, `head`, `dry_run`, `fixer`, `finding`, `reproduction` (`test_files`, `exception`, `crash_file`, `commit`, `reproduced`, `run`), `fix` (`fixer`, `commit`, `files_changed` with `path`, `action` and `by`, `suite`, `panel`) or null, `outcome` (`shipped`, `dry_run` or `stopped`), `stopped`, `pull_request` (`outcome`, `action`, `url`, `branch`, `commit`, `title`, `labels`, `body`) or null, `board_error`, `escalation` (the record's id, or null), `after_escalation` (null when no record was written, else `summary` with `done`, `run_id` and `reason`, and `notify` with `done`, `post` and `reason`), `timing`, `report_path`, `total_cost_usd` and `duration_ms`. `tests/golden/hotfix.json` pins it.

`timing` records the time from the event to the pull request: `event_time` is the ticket's `last seen` time, else its `first seen` (`event_time_source` says which), from the Event section of the body; `pr_opened_at` is when the pull request was opened; `time_to_pr_seconds` is the difference. Each is null when it is not known, and in a dry run. The store has no column for the event's time, so the factory metrics report does not show it; the hotfix reports do.

## Exit codes

| Code | When |
| --- | --- |
| 0 | The pull request was opened, or in a dry run would have been |
| 1 | The run stopped: the test no longer reproduces, the fixer changed nothing or touched a protected file, the suite failed, the panel asked for changes, the budget ran out, a git step failed, or `github.create_pr` refused (a missing `hotfix` label, an existing branch) |
| 2 | Usage: an item `check_hotfixable` refuses, a dirty repository, an `origin` that is not on github.com or not the ticket's, a bad `--work-dir` or `--out-dir` |
| 3 | An unknown item, agent, version or ref |
| 4 | `ANTHROPIC_API_KEY` or `GH_TOKEN` not set, or the sandbox unavailable |

## Tests

`tests/pipelines/test_hotfix.py` runs the pipeline with a fake runner, a fake sandbox, a fake `gh` and a store under a temporary directory, with no key, no network and no Docker. `tests/live/test_hotfix_live.py` runs it as a dry run against the real APIs and a scratch GitHub repository named by `FACTORY_HOTFIX_REPO` (`owner/name`, with a `hotfix` label), and is skipped without it; it is behind the `live`, `live_jev` and `live_github` markers and has not been run as part of #109.

## Known limits

- One panel round. A second round, as the build loop has, would let the fixer answer the panel; it is left out to keep a hotfix small.
- An answered escalation moves the item back to `ready`, but the next hotfix run does not read the answer: the incident ticket is the finding, and there is no `ticket_for_run` step for it. A person reads the answer and acts on it.
- A killed process leaves the item `in_progress`. A person moves it back to `ready`.
- The event time is the latest event Sentry had when `factory incident` ran, not the first event of the incident, so `time_to_pr_seconds` is a lower bound on how long users saw the crash.
- The same limits as the fix loop apply to the clone, the sandbox and the user's own git filters ([fix-loop.md](fix-loop.md#known-limits)).
