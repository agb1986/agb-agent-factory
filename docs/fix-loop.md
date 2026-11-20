# The fix loop

`factory fix` takes one finding of a review, has `test-writer@^0.2` write a test that reproduces it and `fixer@^0.1` change the code until the test passes, runs the whole suite in the sandbox, and puts the change through the review panel of `factory review`. When the panel approves, the change comes out as a patch that `git apply` takes. The code is in `pipelines/fix_loop.py`, with the host side of the sandbox (the clone, its commits and the container) in `pipelines/sandbox.py`. The command line is summarised in [CONVENTIONS.md](CONVENTIONS.md#cli-shape), and the choice of runtime in [ADR 0001](decisions/0001-fixer-runtime.md).

## Usage

```sh
factory review ./repo --base main --head HEAD --json > review.json
factory fix ./repo --finding @review.json --finding-index 0 --json
```

`--finding` is one finding's JSON, a `factory review --json` document, or either one in a file named `@path`. From a review document the finding is the judge's merged finding number `--finding-index` (default 0), without its `sources`. The other options:

| Option | Meaning |
| --- | --- |
| `--head <ref>` | The commit to fix, default `HEAD` |
| `--out-dir DIR` | Where the patch and the report go, default `data/fixes` |
| `--work-dir DIR` | Where the clone is made, default the system temporary directory |
| `--allow-unpublished` | Resolve unpublished versions of the agents too |
| `--budget-usd N` | Cap the spend of the whole run, default `DEFAULT_BUDGET_USD` |
| `--json` | Print the report as one JSON document on stdout and the text on stderr |

`ANTHROPIC_API_KEY` must be set, Docker must be reachable and the sandbox image must be present; see [Sandbox image](#sandbox-image). `TYPESAFE_API_KEY` is used by triage and the judge gate of the panel. Without it both report `jev_error`, the reviewers and the judge run anyway, and the exit code does not change.

## Flow

Every agent call carries the same `pipeline_run_id` and is charged to the same `Budget`.

1. The sandbox is checked (Docker and the image) before any money is spent. The repository must have nothing uncommitted, staged, unstaged or untracked, or the run stops with `DirtyRepositoryError`. Ignored files do not count.
2. The head commit is cloned into a new directory under `--work-dir` with `--no-hardlinks`, checked out detached, and the clone's `origin` remote is removed. Git runs on the host with hooks and fsmonitor off, and its diffs use no external diff driver or text conversion. The source repository gets no commit, ref, hook or config.
3. The test-writer runs over the clone, behind its action guard, and whatever it changed is committed in the clone.
4. The fixer runs over the clone with two hooks in front of its tools: `FilePolicyGuard` over the fixer's file policy, then the action guard. See [The test-file rule](#the-test-file-rule).
5. On the host, every path changed in the clone is checked against the same rule, and then the fixer's work is committed.
6. If the two commits together changed nothing, the run stops with `FixError`.
7. The clone is reset to the fixer's commit and its untracked and ignored files are deleted (`pipelines.sandbox.clean`), so the suite sees the two commits and nothing an earlier command left behind; a clone that cannot be cleaned stops the run with `ToolExecutionError` (exit 1). Then the whole suite, `python -m pytest -q`, runs over the clone in the sandbox with a 600 s limit (`SUITE_TIMEOUT_S`), container start included. A non-zero exit, 124 for a timeout among them, stops the run with `SuiteFailedError`, whose hint carries the end of the output.
8. The panel reviews the diff from the head commit to the fixer's commit in the clone: triage in route-only mode, the three reviewers, the judge gate and the judge, as `factory review` runs them.
9. The patch is written only when the panel approves. The report is written whenever the panel ran.
10. However the run ends, `sandbox.cleanup` kills any container still running over the clone (found by its `factory.sandbox.root` label), and the clone is deleted.

`nothing_to_fix` from the fixer does not stop the run. Whatever the two agents changed, tests alone included, goes to the suite and the panel.

## Outputs

Both files are named after the pipeline run id, in `--out-dir`:

- `<pipeline_run_id>.patch`, the change from the head commit to the fixer's commit, binary-safe, for `git apply` in the source repository. Only when the panel approves.
- `<pipeline_run_id>.json`, the report, the same document `--json` prints: `pipeline_run_id`, `repo_path`, `head`, `finding`, `test_writer` and `fixer` (`ref` and `output`), `files_changed` (each `path`, `action` and `by`, the agents that changed it), `suite` (`argv`, `exit_code`, `passed`, `seconds` and `output_tail`, the last 2,000 characters), `panel` (the `factory review` document), `ship`, `patch_path`, `report_path`, `total_cost_usd` and `duration_ms`.

`tests/golden/fix.json` pins the shape of the JSON document.

## Exit codes

| Code | When |
| --- | --- |
| 0 | The panel approved the fix and the patch was written |
| 1 | The panel did not approve; or `TestFileWriteError`, `SuiteFailedError`, `FixError` (nothing changed), `BudgetExceeded`, or `ToolExecutionError` when git fails on the clone: a clone over the 30 s limit, a clone that cannot be cleaned, a checkout that fails |
| 2 | Usage: a bad option or finding, uncommitted changes (`DirtyRepositoryError`), a `--work-dir` that is not a directory or whose path has a `:`, which Docker cannot mount, an `--out-dir` that cannot be made |
| 3 | An unknown agent, version, repository or ref |
| 4 | `ANTHROPIC_API_KEY` not set, Docker missing or unreachable, or the sandbox image absent |

On exit 1 from the loop itself (not from the panel's verdict) no report is written, since the panel did not run; the trace still has every call.

## The test-file rule

The fixer may change production code only. A path counts as a test, and the fixer may not change it, when:

- `evals.graders.sandbox_tests.is_test_file` says so: anything under a `tests` directory, `conftest.py`, `test_*.py`, `*_test.py`;
- the test-writer changed it, as the commit of its work shows (what it only reports, in `files_changed` or `test_files`, protects nothing);
- it configures the test run: `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `pyproject.toml`, `setup.cfg` or `tox.ini` at the root of the clone;
- it would shadow the test runner: a package at the root named `pytest`, `_pytest`, `pluggy`, `iniconfig` or `packaging`, or a root module of one of those names ending in `.py`, `.pyc` or `.so`. `python -m pytest` puts the working directory first on `sys.path`, so such a module would replace the one in the image. This is best effort: pytest also imports `py` and `pygments`, which are left out because a repository may have a root package of either name. The panel, which reads the whole diff, is the real check.

A refusal names which of these applies. The fixed part of the rule is the `files.deny` block of the fixer's manifest; the fix loop adds the paths the test-writer changed for each run. It is enforced by the pipeline, not by `fs.write_file`, because only the pipeline knows which agent is writing and what the test-writer wrote. The convention is in [CONVENTIONS.md](CONVENTIONS.md#file-policy).

It is checked twice:

- `FilePolicyGuard` (`pipelines/file_policy.py`) is the fixer's hook, so it decides before the action guard. It checks each `fs.write_file` path as written, with `.` and `..` folded, and again with symlinks resolved inside the clone; either match refuses the write.
- After the fixer's run, `check_changed` on the host checks every changed path in the clone, which catches a change made through `shell.run` in the sandbox, where no hook sees the file.

A refusal ends the run with `TestFileWriteError` (exit 1) rather than returning a denial to the fixer: a fixer that wants to change a test has either found a wrong test or is working around it, and both need a person. It also writes one trace row, tagged `fix:test_write_refused`, with `kind` `llm`, the fixer as `agent_ref`, cost 0, the message as `error`, and `decision_json` `{"allow": false, "path", "reason", "check"}`, where `check` is `hook` or `post-check`. The convention is in [CONVENTIONS.md](CONVENTIONS.md#tools).

## Why a clone and not a worktree

The agents need a work tree they may write to, and the sandbox mounts only that work tree, read-write, with its `.git` read-only. A `git worktree` would share the source repository's `.git`: its commits, refs and admin entries land in the source, and its `.git` file points to a directory outside the work tree, where the sandbox cannot follow. A clone with `--no-hardlinks` shares nothing with the source and is deleted whole at the end.

## Sandbox image

The test-writer's and the fixer's commands and the suite run in `factory-sandbox:0.1` unless `FACTORY_SANDBOX_IMAGE` names another image, which the target repository needs when its tests import anything beyond the standard library and pytest. Unset or blank means the default. The container has no network, one CPU, 512 MB of memory and 128 processes, and mounts only the clone. Building a derived image, and running `factory fix` from the deployment image, which has no Docker CLI or socket of its own, are in [deploy.md](deploy.md#sandbox-containers).

When `factory fix` runs inside a container that talks to the host's Docker daemon, the clone must be on a path the host sees at the same place: pass `--work-dir` a directory mounted at the same path on both sides.

## Cost and trace

Every call is a row in the trace store under the run's `pipeline_run_id`: the test-writer's and the fixer's turns, the action guard's decisions, triage, the reviewers, the judge gate and the judge. `total_cost_usd` in the report is `Trace.cost_by_pipeline_run`, and `factory cost --since <date> --by pipeline` lists the run among the others.

## Known limits

- Nothing checks that the test-writer's tests fail on the head commit. The test-writer is told to run them and see them fail, but the pipeline does not rerun them before the fixer starts, so a test that passes on the unfixed code reaches the panel unremarked.
- Nothing stops the test-writer from changing production code. Whatever it changed becomes protected from the fixer, so such a change shows in the report's `files_changed` under `test-writer` and goes to the panel.
- The clone runs under git's 30 s limit (`tools._common.TIMEOUT_S`), so a very large repository fails with a `ToolExecutionError` rather than cloning slowly.
- `nothing_to_fix` does not end the run, as above; a finding the fixer judges wrong still costs a suite run and a panel.
- A fix that needs a change to `pyproject.toml` or `setup.cfg`, such as a new dependency, is refused like a change to a test and needs a person.
- The code under test runs in the suite's own process, so a fix can still skip tests or end the run early from production code. The panel, which reads the whole diff, is the check on that.
- Filters configured in the user's own git config, such as Git LFS, can run on the source repository's status and on the clone's checkout and commits when the repository's `.gitattributes` names them. They are the user's and trusted. The clone has no config beyond git's defaults, so the repository brings no filter, hook or driver of its own.
- `sandbox.cleanup` finds containers through `docker ps`. If the daemon fails at that moment, a container can outlive the run until its command ends; it still carries the `factory.sandbox.root` label, so `docker ps --filter label=factory.sandbox.root` finds it.
- The live end-to-end test, `tests/live/test_fix_loop_live.py`, runs on the `off-by-one-red` fixture only.
