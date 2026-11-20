# The build loop

`factory build` takes one `ready` work item to a reviewed pull request. The coder for the ticket's kind builds the ticket in a throwaway clone with its file policy enforced on the host, the whole suite runs in the sandbox, and the review panel of `factory review` reviews the change. When the panel ships it, `github.create_pr` opens the pull request on the repository's `origin` and the item moves to `in_review`. The code is in `pipelines/build_loop.py`. It composes the `dev` team (the coder) and the `qa` team (the panel), and no model moves the work item. The work items are described in [work-items.md](work-items.md) and [CONVENTIONS.md](CONVENTIONS.md#work-items), the outside write in [ADR 0003](decisions/0003-outside-writes.md), and the sandbox and the clone in [fix-loop.md](fix-loop.md).

## Usage

```sh
factory ticket ready
factory build wi-4 --repo ./widgets --dry-run --json
factory build wi-4 --repo ./widgets --json
```

| Option | Meaning |
| --- | --- |
| `<id>` | The work item, `wi-<n>`. It must be `ready` |
| `--repo PATH` | The git repository to build in. Default: the ticket's `repo` when it is an absolute path, else the working directory |
| `--dry-run` | Run every agent, the suite and the panel, but open no pull request and move no work item |
| `--out-dir DIR` | Where the report goes, default `data/builds` |
| `--work-dir DIR` | Where the clone is made, default the system temporary directory |
| `--budget-usd N` | Cap the spend of the whole run, default `DEFAULT_BUDGET_USD` |
| `--allow-unpublished` | Resolve unpublished versions of the agents too |
| `--json` | Print the report as one JSON document on stdout and the text on stderr |

`ANTHROPIC_API_KEY` and `GH_TOKEN` must be set, a dry run included, since a dry run still reads the target repository. Docker must be reachable and the sandbox image present, as for `factory fix`. `TYPESAFE_API_KEY` is used by triage and the judge gate of the panel. Without it both report `jev_error`, and the reviewers and the judge run anyway.

The ticket's kind picks the coder from the fixed table `CODERS`. `feature` and `bug` use `feature-coder@^0.1`. `refactor` uses `refactorer@^0.1` and goes through [the behaviour check](#the-behaviour-check-refactor). `docs` uses `feature-coder@^0.1` until the documentation agent (#97) exists. `incident` is refused with a pointer to the hotfix pipeline (#109), and `research` is refused with a pointer to `factory run`.

## Flow

Every agent call, every move of the item and the outside write carry the same `pipeline_run_id`, and every agent call is charged to the same `Budget`.

1. **Preflight.** Nothing is written in this step, and any failure leaves the item untouched.
   - The item must exist and be `ready`, and its kind must have a coder.
   - The agents must resolve. The coder must be an LLM agent sandboxed offline, with an action guard and a `files` policy.
   - Both keys must be set.
   - The repository must have nothing uncommitted.
   - Its `origin` must be on github.com, and, when the ticket's `repo` is `owner/name`, it must be that repository. When the ticket names a local path, `--repo` must be that path or be left out.
   - The ticket's `ref` must resolve to a commit, and the work directory and the output directory must be usable.
   - The sandbox must be available.
2. **Start.** The run is linked to the item with `link_run`. Unless it is a dry run, the item then moves to `in_progress`, before the clone is made.
3. **Build.** The commit at `ref` is cloned under `--work-dir`, as the fix loop clones (`pipelines.sandbox.checkout`). For a `refactor` ticket the suite is probed here, before the coder's first round ([the behaviour check](#the-behaviour-check-refactor)). The coder gets the ticket and, for each dependency, a line `wi-<n>: <title>`. It runs behind `FilePolicyGuard` over its manifest's `files` policy and then its action guard. After the run, `check_changed` checks every changed path on the host. A coder answer with `blocked` set ends the run with `CoderBlockedError`.
4. **Commit and test.** The coder's work is committed in the clone. If it changed nothing, the run ends with `BuildError`. The clone is cleaned, and `python -m pytest -q` runs over it in the sandbox, with the fix loop's 600 s limit. A non-zero exit ends the run with `SuiteFailedError`. For a `refactor` ticket a commit that changed a test file is refused before the suite runs, and the suite is probed again after it passes.
5. **Review.** The panel reviews the diff from the `ref` commit to the coder's commit: triage, the reviewers, the judge gate and the judge, as `factory review` runs them.
   - When the panel asks for changes, the coder runs once more in the same clone. Its ticket has the panel's findings appended to the body, and one acceptance criterion is added saying they are addressed. That run's parent is the judge's row. Steps 3 to 5 then repeat.
   - When the panel asks for changes a second time, the run ends with `ChangesRequestedError` (`MAX_ROUNDS` is 2).
6. **Ship.**
   1. The coder's last commit is fetched from the clone into the source repository's object store. No ref, branch or config is made.
   2. `runtime.outside.write("github.create_pr", ...)` runs for the judge's manifest, with the judge's row as the parent. It pushes `factory/<pipeline_run_id>` from the source repository and opens the pull request.
   3. The item moves to `in_review`, with the pull request's URL in the reason.
7. **End.** Every end after preflight other than a shipped change moves the item out of `in_progress`, in a `finally` block. A stop a person can answer (a `blocked` coder, a budget stop, a second request for changes, a coder that changed nothing, or a failed check) writes an escalation record and moves the item to `escalated` in one step, through `runtime.escalations.escalate`. Every other stop, and a crash, moves it to `blocked`, and so does a stop whose record cannot be written, with the report's `board_error` saying why. The reason is `<ErrorClass>: <message>; report <path>` either way. [CONVENTIONS.md](CONVENTIONS.md#escalations) has the table of every stop and where it goes. The report's `escalation` is the record's id, or null. The container and the clone are removed however the run ends, and the report is written last.

The pull request's body holds the ticket's body, its acceptance criteria, the coder's summary and the line ``Work item `wi-<n>`.``. `github.create_pr` adds the pipeline run id and its marker below that.

### Why the commit is fetched into the source repository

ADR 0003 requires an outside write to target the pipeline's own repository, which `OutsideContext.for_repository` reads from the source repository's `origin`. The clone has no `origin` and is deleted at the end. So `create_pr` pushes from the source repository, which must hold the commit. `_fetch_into` fetches that one object with hooks and fsmonitor off and writes no ref, so the source repository's branches, tags and config stay as they were. After the push the source repository still has no new branch and no new tag, but git may record the remote-tracking ref `refs/remotes/origin/factory/<pipeline_run_id>` for the pushed branch, depending on the git version. The unreferenced objects are left for `git gc`.

### Dry runs

A dry run does everything a real run does except write: the agents, the suite, the panel and the reads of `create_pr`, which checks the target repository and the branch. It opens no pull request and moves no work item. ADR 0003 left the question of moving the item to this issue. The item stays `ready` because a dry run is a rehearsal, and a `ready` item that a dry run had moved to `in_review` or `blocked` would need a person to move it back. The run is still linked to the item, so `factory ticket show` counts what the dry run cost. The report's `pull_request` carries the planned branch, title and body, with `url` null and `outcome` `dry_run`.

## The file policy

The refactorer's manifest also denies every test path (`**/tests/**`, `**/test_*.py`, `**/*_test.py`, `**/conftest.py`), so it may write production code only. The feature-coder may write tests and code. Its manifest's `files.deny` keeps version control, CI, pytest's root configuration (`pyproject.toml`, `setup.cfg`, `pytest.ini` and the like) and modules that would shadow pytest out of its reach. The build loop is the first pipeline to enforce it, with the same two checks as the fix loop ([CONVENTIONS.md](CONVENTIONS.md#file-policy)):

- `FilePolicyGuard` is the coder's hook. It refuses an `fs.write_file` to a denied path, both as the path is written and with symlinks resolved.
- `check_changed`, on the host after the coder's run, refuses a denied path that changed by another route, such as `shell.run`.

A refusal ends the run with `FileWriteRefusedError` (exit 1). It writes one trace row:

| Field | Value |
| --- | --- |
| `tag` | `build:file_write_refused` (`FILE_WRITE_REFUSED_TAG`) |
| `kind` | `llm` |
| `agent_ref` | The coder |
| `cost_usd` | 0 |
| `error` | The message |
| `decision_json` | `{"allow": false, "path", "reason", "check"}`, where `check` is `hook` or `post-check` |

## The behaviour check (refactor)

A refactor must not change behaviour. The refactorer's prompt says so, and `pipelines/behaviour.py` checks it, since the prompt alone proves nothing. `KIND_CHECKS` in `pipelines/build_loop.py` maps a ticket kind to a check with three steps, and only `refactor` has one, `BehaviourCheck`. The `feature`, `bug` and `docs` paths are unchanged.

A **probe** is one sandbox command that runs the whole suite and measures coverage in the same run. It is `python -c` with `evals.coverage.MEASURE_SCRIPT`, which runs pytest in process under the standard library's line tracer, so the sandbox image is unchanged and needs no plugin. Pytest runs with `-q -rpfE --tb=no -p no:cacheprovider`, so its short summary lists every passed, failed and erroring test by node id. The per-test states are read from the lines after the last summary header. Before each probe the clone is cleaned, as before the suite.

The check's steps run at these points of the loop:

1. **Before the first round** (`before`), at the `ref` commit. If the suite does not exit 0, or no test passes, the run ends with `BaselineNotGreenError`, before the coder runs. A refactor is judged against tests that pin today's behaviour, and a red or empty suite pins nothing. The message names the commit and up to five failing tests.
2. **After each round's commit** (`after_commit`). A commit that changed a test file, by the rule of `evals.graders.sandbox_tests.is_test_file`, ends the run with `TestFileChangedError`, naming the files. The refactorer's file policy refuses the usual test paths first, with `FileWriteRefusedError`. This step catches the rest, such as a data file under `tests/`, or a coder whose policy is looser.
3. **After each passing suite** (`after_suite`), at the round's commit, before the panel. The round's report entry gets `behaviour`, then the two probes are compared:
   - A test that passed before and fails, errors, is skipped or is missing after ends the run with `BehaviourChangedError`. The message names up to five of them with what each did and counts the rest. A test that did not pass before is not compared, and a new test may do anything.
   - A probe that exits non-zero with every earlier test still passing also ends with `BehaviourChangedError`.
   - More uncovered production statements than before ends with `CoverageDroppedError`. The message gives both counts and both percentages.
   - Coverage measured before but not after ends with `CoverageUnmeasuredError`.

Every refusal is a `BehaviourError` and a stop like the others: exit 1, the error class and message in the reason, and the report written with the round as far as it ran. `BaselineNotGreenError` moves the item to `blocked`, because the repository needs fixing before any coder runs; the other four are about the coder's change and move it to `escalated` with trigger `check_failed`.

### How coverage is measured

- **What is measured.** The targets are the tracked `.py` files that are not tests, listed on the host with `git ls-files` at each probe's commit. A new helper module the refactor adds is therefore counted after. The list travels inside the script, since the sandbox allows 64 arguments.
- **What is compared.** The count of uncovered statements, not the ratio. Removing duplicate code removes covered lines and so lowers the ratio of a change that loses nothing. A statement is a line with bytecode, as `evals/coverage.py` describes.
- **When it cannot be measured.** Coverage is unmeasured when there is no production `.py` file, when the list does not fit in one sandbox argument (`MAX_SCRIPT_CHARS`, 32,768 characters), or when the run printed no coverage report. An unmeasured baseline skips the coverage rule for the run, and the test-state rule still applies. An unmeasured probe after a measured baseline is a refusal, since a refactor that breaks the measurement could hide a drop.

### The coverage gap

The coverage rule is a coarse guard, and these changes pass it:

- **Line coverage only.** A line that runs counts as covered even when only one side of a condition on it ran.
- **A count, not a set of lines.** Coverage that moves from one line to another, with the same number uncovered, is not seen. Deleting uncovered dead code lowers the count and can hide a drop of the same size elsewhere.
- **One process only.** Code the suite runs in a subprocess, and compiled extensions, are not traced.
- **Unmeasured baseline.** A repository with too many production files for one argument, or with none, is not checked for coverage at all.

The test states are the main guard. The eval's deterministic check of the public names and signatures (`evals/public_api.py`) is not run by the loop, which leaves a signature change to the panel.

## Outputs

The report is `<pipeline_run_id>.json` in `--out-dir`, the same document `--json` prints. It is written on every end after preflight. Its keys are:

| Key | Contents |
| --- | --- |
| `pipeline_run_id` | The run |
| `work_item` | `id`, `status_before`, `status_after` and `ticket` |
| `repo_path` | The source repository |
| `head` | The commit at the ticket's `ref` |
| `dry_run` | Whether this was a dry run |
| `coder` | The coder's address |
| `rounds` | One entry per round: `number`, `coder` (`run_id` and `output`), `commit`, `files_changed` (`path`, `action`), `suite` (`argv`, `exit_code`, `passed`, `seconds`, `output_tail`) and `panel` (the `factory review` document). A `refactor` round also has `behaviour`: `before` and `after` (each `exit_code`, `tests` counts of `passed`, `failed` and `error`, `statements`, `uncovered`, `unmeasured` and `seconds`) and `tests_changed` (`test`, `after`). Other kinds have no `behaviour` key. A round that stopped early keeps what ran and nulls the rest |
| `outcome` | `shipped`, `dry_run` or `stopped` |
| `stopped` | Null, or the stop's `type`, `message`, `hint` and `exit_code` |
| `pull_request` | Null, or `outcome` (`written` or `dry_run`), `action`, `url` (null in a dry run), `branch`, `commit`, `title` and `body` |
| `board_error` | Null, or the error when the final move of the item failed |
| `report_path` | Where the report was written |
| `total_cost_usd` | `Trace.cost_by_pipeline_run` |
| `duration_ms` | How long the run took |

`tests/golden/build.json` pins the shape of the document.

## Exit codes

| Code | When |
| --- | --- |
| 0 | The pull request was opened, or in a dry run would have been |
| 1 | The run stopped: `ChangesRequestedError`, `CoderBlockedError`, `SuiteFailedError`, `FileWriteRefusedError`, a `BehaviourError` of a `refactor` run (`BaselineNotGreenError`, `TestFileChangedError`, `BehaviourChangedError`, `CoverageDroppedError`, `CoverageUnmeasuredError`), `NoChangeError` (nothing changed), `BuildError` (the fetch failed), `BudgetExceeded`, or an outside write that was refused or failed |
| 2 | Usage: a bad option, an item that is not `ready`, a kind the loop does not take, uncommitted changes, an `origin` that is missing, not on github.com or not the ticket's repository, a `--repo` that is not the ticket's path, a `--work-dir` that is not a directory or has a `:` in its path, an `--out-dir` that cannot be made |
| 3 | An unknown work item, agent, version or ref, or a `--repo` that does not exist |
| 4 | `ANTHROPIC_API_KEY` or `GH_TOKEN` not set, Docker missing or unreachable, or the sandbox image absent |

Codes 2, 3 and 4 come from preflight, so the item is untouched and no report is written. On code 1 the item is `escalated` or `blocked` (unless it was a dry run) and the report says why.

## Tests

`tests/pipelines/test_build_loop.py` runs the loop with a fake coder, a fake panel, a fake sandbox and the fake `gh` of `tests/gh_fake.py`, and covers every stop, the dry run, preflight and the exit codes of `factory build`. `tests/pipelines/test_behaviour.py` covers the behaviour check: the parsing and the rules on their own, one real probe of a built fixture run on the host, and `refactor` runs of the loop whose sandbox answers each probe with scripted output.

`tests/live/test_build_loop_live.py` is a live dry run against the real Messages API, the Jev API and github.com, with Docker and the sandbox image. It needs `FACTORY_LIVE=1`, `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY` and `GH_TOKEN`:

```sh
FACTORY_LIVE=1 uv run pytest tests/live/test_build_loop_live.py -m "live and live_jev and live_github"
```

## Known limits

- A contradiction between the ticket and the code is detected only when the coder changes nothing. A coder that changes the code against the ticket's intent is caught by the suite and the panel, not by an escalation.
- A killed process leaves the item `in_progress`. A person moves it back to `ready`.
- The second round starts from the first round's commit in the same clone and is reviewed as one diff from `ref`. Nothing checks that the coder addressed each finding beyond the panel's second review.
- The coder's tests are not checked to fail before its change. The suite and the panel are the checks.
- A `refactor` run probes the whole suite twice more than the other kinds do, under a line tracer, which is slower than the plain suite. Each probe has the sandbox's 600 s limit.
- The behaviour check has the coverage gap described above, and the loop does not compare public signatures.
- The same limits as the fix loop apply to the clone, the sandbox and the user's own git filters ([fix-loop.md](fix-loop.md#known-limits)).
