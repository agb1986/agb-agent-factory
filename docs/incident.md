# The incident pipeline

`factory incident` takes one production event to a reproducible incident ticket in the work-item store (#108). It composes two agents and the store: `triage-diagnostics` (team `sre`) reads the repository at the release the event came from and diagnoses it, `test-writer` (team `qa`) writes a test that should fail the way the event did, and the pipeline runs that test in the offline sandbox and files one ticket of kind `incident`. The code is `pipelines/incident.py`. The store is described in [work-items.md](work-items.md) and the rules it follows are in [ADR 0002](decisions/0002-work-items.md).

The agents write nothing. triage-diagnostics has read-only tools (`git.read_file`, `rg.search`, `deps.graph`, `deps.impact`) and no sandbox, and test-writer writes only inside a throwaway clone. The pipeline is the only writer of the store, with actor `pipeline:incident`. Nothing is committed to, pushed from or merged into the source repository, and nothing is written to Sentry or GitHub ([ADR 0003](decisions/0003-outside-writes.md)).

## The steps

```console
$ factory incident ./repo --event @issue.json
incident run <run-id>: ZeroDivisionError in average_rating for a product with no ratings
  release head (f2135236fa26)
  suspected: reviews/scores.py, reviews/listing.py
  confidence 0.90
  reproduced by tests/test_no_ratings.py
filed wi-1 (ready)
report data/incidents/<run-id>.json
cost $0.0000
```

1. The event is read and validated, and the release it came from is resolved to a commit (see below). Both happen before any model call, so a bad event or an unknown release costs nothing.
2. The repository is cloned into a throwaway directory (`pipelines.sandbox.checkout`) at that commit. Nothing reads the source work tree after this, so uncommitted changes in it are never seen and do not stop the run.
3. triage-diagnostics runs on the clone with the event and the commit. It returns a title, a summary, the reproduction steps, the suspected paths (the culprit file and what `deps.impact` says depends on it), the culprit lines, a confidence and a draft test.
4. test-writer runs unchanged, in `reproduce` mode, on a finding built from the diagnosis and the event: severity `blocker` for a `fatal` event and `major` otherwise, category `crash`, and a body with the steps, the stack trace, the reproduction rule and the draft test. Its run is a child of the triage run (`parent_run_id`). Its work is committed in the clone.
5. The test files it created or modified run in the offline sandbox, with `pytest -q --tb=short -p no:cacheprovider` and the files. The result is checked against the rule below.
6. The pipeline files one ticket, links the run to it and writes the JSON report to `data/incidents/<run-id>.json`. The clone and the sandbox are removed whatever happened.

Both agent runs carry one pipeline run id, so `Trace.cost_by_pipeline_run` gives the whole cost, and the run is linked to the ticket with `link_run`. A run that fails (an agent error, a budget overrun, a sandbox that cannot start) files nothing.

## The event

The input is one Sentry issue as a JSON document in the shape `sentry.read_issue` returns: `id`, `short_id`, `title`, `culprit`, `level`, `status`, `count`, `user_count`, `first_seen`, `last_seen`, `release`, `first_release`, `last_release`, `event_id`, `exceptions` and `breadcrumbs`. Each exception has `type`, `value` and `frames`, and each frame has `file`, `function`, `line` and `in_app`. Frames run outermost first, and the last exception is the one that was raised. The model is `SentryEvent` in `agents/triage-diagnostics/schemas.py`, and an unknown key is rejected.

`--event` takes the JSON itself or `@FILE`. The pipeline never calls Sentry and never needs a Sentry token: a person or an earlier step saves the issue, and the pipeline reads the saved document. This keeps the agent read-only, keeps the tests and evals on hand-written payloads, and means the event a ticket was filed from is in its report.

The crash file is the file of the innermost frame marked `in_app` (or, if none is, the innermost frame not marked `in_app: false`). Its path in the event is the path on the production host, such as `/app/reviews/scores.py`, so it is mapped to the longest trailing part that is a file of the clone, here `reviews/scores.py`. A path with `..` never maps. When no frame maps, the event cannot be reproduced, and the ticket says so.

## The release

The diagnosis and the reproduction run against the code the event came from, not against the current branch. The commit is found in this order:

1. `--ref`, when given. A ref that names no commit exits 3.
2. The event's `release`, as a tag or commit of the repository.
3. `v<release>`, for a repository that tags releases with a `v` prefix.

If the event names no release, or names one that is none of these, the command exits 2 before any agent runs and says to pass `--ref`. It does not fall back to `HEAD`, because a diagnosis against the wrong code is worse than none.

## What counts as a reproduction

The test files test-writer wrote reproduce the event when all three hold when they run at the release:

- pytest exits 1: tests ran and at least one failed. A collection error or an internal error does not count.
- The output has an `E   <Exception>` line for the event's exception class. A module prefix is allowed (`E   decimal.InvalidOperation` matches `InvalidOperation`).
- The traceback has a line in the crash file (`reviews/scores.py:7`).

So a test that fails on a wrong assertion, or that raises the right exception from some other file, does not count. The rule is `evals.graders.incident.reproduces`, and the eval applies the same function, so what the pipeline calls a reproduction is what the eval scores. When it holds, the confidence is raised to at least `REPRODUCED_CONFIDENCE` (0.9) and the test's code goes into the ticket. When it does not, the confidence stays the diagnosis confidence and the ticket says why: no test file written, no frame mapped, or the tests did not fail that way (with the exit code).

## The ticket

One ticket, kind `incident`, with:

- `title`: the diagnosis title.
- `body`: the layout below.
- `acceptance`: two criteria. When the event reproduced: `<test files> passes, where it fails at <release> with <Exception>.` and `The whole test suite passes.` Otherwise: `A test fails at <release> with <Exception> raised from the file the event shows, and passes after the fix.` and `The whole test suite passes.`
- `repo`: `--repository` when given, else the repository's absolute path.
- `ref`: the release commit.
- `impacted_paths`: the suspected paths.

It is added in `todo` and, when the event reproduced, moved to `ready` with the reason `<files> reproduces the event at <release>`. The add carries the reason `filed from Sentry issue <id> at release <release>`. No new Ticket field holds the trace, the steps or the confidence: they are sections of the body, and the suspected paths are also `impacted_paths`.

### The body layout

The hotfix pipeline (#109) reads the body, so its layout is fixed. It has exactly these level-two headings, in this order, each once: `BODY_SECTIONS` in `pipelines/incident.py`.

| Section | Content |
| --- | --- |
| `## Event` | Bullets: `- Sentry issue: <short_id>`, `- Title:`, `- Level:`, `- Release: <release> (commit <sha>)`, `- Exception: <Type>: <value>`, `- Crash file: <path>` (or `unknown`), `- Events: <count>, <users> users; first seen <t>, last seen <t>`, and `- Latest event: <event_id>` when the event has one |
| `## Summary` | The diagnosis summary, one paragraph |
| `## Stack trace` | A `text` fence holding a Python-style traceback built from the event's frames, outermost first, ending with `<Type>: <value>` |
| `## Reproduction steps` | A numbered list |
| `## Suspected paths` | One bullet per path, in backticks, in the agent's order |
| `## Culprit` | `` `<file>` line <n>: <reason> `` or `` `<file>` lines <a>-<b>: <reason> `` |
| `## Confidence` | Bullets: `- Confidence: 0.90`, `- Reproduced: yes` or `no`, `- Diagnosis confidence: 0.75` |
| `## Reproduction test` | When reproduced: `` `<files>` fails at <release> with <Exception> raised from `<crash file>`. ``, then for each file `` `<path>`: `` and a `python` fence of its code (the fence is longer than any run of backticks in the code). Otherwise: `No test reproduces the event yet: <why>` |

`incident_sections(body)` splits a body back into `{heading: text}` and raises `ValueError` unless the headings are exactly `BODY_SECTIONS`; a `##` line inside a fence is not a heading. `is_reproduced(body)` is true when the Confidence section says `- Reproduced: yes`. A consumer should use these two and not parse the Markdown itself; a change to the layout changes `BODY_SECTIONS` and the golden file with it.

### `ready` and an unreproduced incident

`factory ticket ready` promotes every `todo` item whose dependencies are all `done`, and an incident has none, so it also promotes an incident that did not reproduce. `ready` therefore does not mean "reproduced" for an incident. The hotfix pipeline must check `is_reproduced(body)` before it acts, and send an unreproduced incident to a person. The rule in `runtime/work_items.py` is unchanged.

## Exit codes and the JSON document

| Exit | When |
| --- | --- |
| 0 | A ticket was filed, whether the event reproduced or not; the ticket's status says which |
| 1 | The run failed or went over budget; nothing was filed |
| 2 | Usage: `--event` is not JSON, not an object or not a valid issue; the event names no release, or one that is not a tag or commit and no `--ref` was given; `--work-dir` is not a directory or its path has a `:`; an agent is not the kind the pipeline needs |
| 3 | The repository, the `--event` file, an agent or the `--ref` commit does not exist |
| 4 | `ANTHROPIC_API_KEY` is not set (checked after the event and the release), or Docker or the sandbox image is missing |

Under `--json`, stdout carries one document and the text goes to stderr. The document has `pipeline_run_id`, `repo_path`, `release`, `commit`, `event`, `triage` and `test_writer` (each `ref` and `output`), `reproduction` (`test_files`, `exception`, `crash_file`, `reproduced`, `why_not`, and `run`: `argv`, `exit_code`, `seconds`, `output_tail`, or null when no test ran), `confidence`, `ticket` (`id`, `status`, `kind`, `title`, `body`, `acceptance`, `repo`, `ref`, `impacted_paths`), `report_path`, `total_cost_usd` and `duration_ms`. The same document is the report file. `tests/golden/incident.json` pins it.

## Options

`factory incident <repo> --event JSON|@FILE [--ref REF] [--repository NAME] [--out-dir DIR] [--work-dir DIR] [--json] [--allow-unpublished] [--budget-usd N]`. `--out-dir` is where the report goes (default `data/incidents`), and `--work-dir` where the clone is made (default a temporary directory). Both agents (`triage-diagnostics@^0.1`, `test-writer@^0.2`) are resolved before the first request, published versions only unless `--allow-unpublished`, and the pipeline refuses a triage agent with a sandbox or a files policy and a test-writer that is not offline-sandboxed. The run is charged to one `Budget`.

## Testing

`tests/pipelines/test_incident.py` runs the pipeline with a fake runner, a fake sandbox and a DuckDB file in a temporary directory, with no key, no network and no Docker. `tests/evals/test_incident_grader.py` tests the reproduction rule. The four eval cases of triage-diagnostics are fixtures with a planted crash and a hand-written event (`crash-empty-average`, `crash-first-of-empty`, `crash-loader-caller`, whose fault is above the raising frame, and `crash-missing-config-key`); the `incident` grader is described in [evals.md](evals.md). `tests/live/test_triage_diagnostics_live.py` runs the agent and the whole pipeline against the real API; it is behind the `live` marker and has not been run as part of #108, nor have `factory eval triage-diagnostics` and `factory publish-check triage-diagnostics`.
