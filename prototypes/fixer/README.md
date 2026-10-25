# Fixer runtime prototype

**Not production code.** This directory is the throwaway evidence for [ADR 0001](../../docs/decisions/0001-fixer-runtime.md) (#21). Nothing outside `prototypes/` imports it, the default `uv run pytest` does not collect it, and it can be deleted once #22 to #24 have landed.

Both runners build the `off-by-one` fixture into a fresh temporary directory with `fixtures.build.build(["off-by-one"], dest=...)` and hand the fixer the fixture's one defect, `last-window-dropped`, as a finding. The fixer edits the work tree. The runner then writes the change as `off-by-one.patch` next to the repository and checks the fix in the sandbox, running both the defect's own example and the fixture's test suite. Last, it compares this repository's `git status` before and after the run, to show that nothing outside the temporary directory changed.

| File | Role |
| --- | --- |
| `Dockerfile` | Sandbox image `factory-fixer-sandbox:proto`: `python:3.12-slim` with `pytest==9.1.1` |
| `sandbox.py` | Runs one allow-listed command (`python` or `pytest`) in a fresh container. It has no network, a read-only root, capped CPU, memory and pids, no capabilities and the host uid. The work tree is the only writable mount, with `.git` mounted again read-only. On timeout it kills the container by name |
| `fixer_tools.py` | Option (a) tools: `fs.list_files`, `fs.read_file`, `fs.write_file` (confined to the work tree, never `.git`, at most 256 KiB) and `shell.run` (runs in the sandbox) |
| `agent/` | `fixer-proto@0.0.1`: the manifest, the system prompt and the input and output models |
| `run.py` | Option (a): the unchanged `runtime.runner.run` (Tool Runner), with a recording `ToolHook` where the action guard of #23 attaches |
| `sdk_run.py` | Option (b): Claude Agent SDK 0.2.174 driving the CLI's `Read`, `Write`, `Edit` and `Bash`, with a `PreToolUse` guard that confines paths and rewrites `Bash` into the same `docker run` |
| `test_fixer_sandbox.py` | Isolation tests for the tools and the sandbox: escapes, symlinks, `.git`, no network, a read-only root, file ownership and the timeout kill |
| `test_fixer_sdk_guard.py` | Tests of the option (b) guard and recorder; they need neither the SDK nor Docker |
| `test_fixer_live.py` | Live end-to-end tests: the manifest model is served, and options (a) and (b) each fix the fixture |

Option (c), Managed Agents, has no prototype. The ADR explains why.

## Run it

Building the image needs the network; running it never does.

```sh
docker build -t factory-fixer-sandbox:proto prototypes/fixer

uv run python -m prototypes.fixer.run [--dest DIR]
uv run --with claude-agent-sdk==0.2.174 python -m prototypes.fixer.sdk_run [--dest DIR]
uv run --with claude-agent-sdk==0.2.174 python -m prototypes.fixer.sdk_run --probe-sandbox
```

Each runner prints one JSON object with the checks, the cost, the token counts, the tool calls, the changed files, the patch path and `factory_tree_unchanged`, and exits 0 only if the fix holds. The run's trace row is written to `DIR/trace.duckdb`. A failed run is evidence too: its error lands under `error` instead of being raised. The SDK is not a project dependency, so option (b) runs only under `uv run --with`.

```sh
uv run pytest prototypes/fixer -m "not live"                                    # offline
uv run --with claude-agent-sdk==0.2.174 pytest prototypes/fixer -m live         # a few cents
```

The live tests skip themselves when `ANTHROPIC_API_KEY` is unset or the image is missing, and the option (b) test skips itself without the SDK. Without `-m`, a plain `uv run pytest prototypes/fixer` also runs the live tests whenever the key is set.

## Results

All runs were on 2026-10-25 against `claude-sonnet-5-5` at effort `xhigh`, using Docker 29.1.3 with cgroup v2. Option (b) used the Claude Code CLI 2.1.310 that the SDK bundles. Cost is computed by `runtime.pricing` from the summed usage, at $2 input, $10 output, $0.20 cache read and $2.50 cache write per million tokens. `agent s` is the fixer alone; `wall` is the whole process, checks and patch included.

### Option (a): Tool Runner and the hand-built sandbox (3 of 3 fixed)

| Run | agent s | wall s | cost USD | input | output | cache read | cache write | tool calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 16.3 | 19.40 | 0.0266 | 4,957 | 1,184 | 5,832 | 1,458 | 7 |
| 2 | 13.0 | 15.63 | 0.0231 | 4,957 | 1,170 | 7,290 | 0 | 7 |
| 3 | 13.7 | 16.33 | 0.0231 | 4,957 | 1,168 | 7,290 | 0 | 7 |

All three runs followed the same steps. There were no tool errors, and the two sandboxed `python -m pytest -q` runs took 1.2 to 1.6 s together. The fixer wrote `tests/test_rolling.py` first and `series/rolling.py` second. Both checks exited 0, the factory tree was unchanged, and peak RSS was 162 MB. The patch:

```diff
-    for end in range(window, len(values) - 1):
+    for end in range(window, len(values)):
```

It also adds `test_includes_last_window`, which asserts `rolling_sums([1, 2, 3, 4], 2) == [3, 5, 7]`.

### Option (b): Claude Agent SDK (4 of 4 fixed the code)

| Run | agent s | wall s | CLI API ms | turns | cost USD | CLI estimate USD | output | cache read | cache write | tool calls | denied by the guard |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 33.1 | 35.33 | 30,300 | 14 | 0.0768 | 0.0691 | 3,758 | 76,997 | 9,520 | 12 | `find`, `ls -R`, `git ls-files`, `StructuredOutput` ×2 |
| 2 | 20.0 | 22.14 | 16,687 | 11 | 0.0362 | 0.0318 | 1,841 | 44,052 | 3,591 | 10 | `find`, `git ls-files` |
| 3 | 17.0 | 19.12 | 14,437 | 10 | 0.0332 | 0.0300 | 1,813 | 31,375 | 3,493 | 9 | `ls -R` on an absolute host path |
| 4 | 21.9 | 23.99 | 18,497 | 12 | 0.0391 | 0.0335 | 1,874 | 56,505 | 3,621 | 11 | `find`, `git ls-files` |

Uncached input was 12 to 28 tokens per run. Every run changed the same two files with an equivalent patch, both checks exited 0, the factory tree was unchanged, and peak RSS was 245 to 254 MB. The live test `test_option_b_fixes_the_fixture` passed after these runs.

### CLI sandbox probe

`--probe-sandbox` asked the CLI to start with its own Bash sandbox required (`enabled`, `failIfUnavailable`, `allowUnsandboxedCommands: false`). It refused to start:

```text
Sandbox required but unavailable: sandbox is enabled but dependencies are missing: socat not installed · install missing tools (e.g. apt install bubblewrap socat) ... Set sandbox.failIfUnavailable=false to allow unsandboxed execution.
sandbox.failIfUnavailable is set — refusing to start without a working sandbox.
```

The host has `bwrap` but not `socat`. This prototype does not install host packages, so option (b) uses the `PreToolUse` guard and Docker instead of the CLI sandbox.

## Failures and surprises, kept as evidence

- **Option (b) run 1 returned no report.** The guard denied every tool it did not know, and that included `StructuredOutput`, the tool the CLI adds for `output_format`. The model called it twice and was refused both times, yet the CLI still ended with `subtype: success`, `is_error: false` and no `structured_output`. The guard now allows that tool, and `fix` validates the output with `FixReport.model_validate` and records an error when it is missing. Under the current code, run 1 would be recorded as failed even though its patch was correct.
- **The model reaches for shell tools first.** In option (b) it started with `find`, `ls -R` or `git ls-files` in every run, and the guard denied them. It then listed files through `python -c "import os; ... os.walk(...)"`, which the allow-list cannot tell apart from any other Python, and which ran in the sandbox. A hook denial reaches the model as `PreToolUse:Bash hook error: program not allowed: 'find'`. Option (a) offers `fs.list_files`, so the model never tried a shell listing.
- **Absolute host paths leak into commands.** In run 3 the model ran `ls -R` on the work tree's absolute host path. In option (b) the CLI tells the model its host `cwd`, while the sandbox sees `/work`. A guard that rewrites commands has to deal with host paths, and option (a) never shows them.
- **Parallel tool calls.** In both options the model asked for several tool calls in one turn. The fix loop has to run them in order, because one may write a file that the next one tests.
- **The CLI writes state of its own.** With an empty `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, it still wrote `.claude.json`, `policy-limits.json` with signature and stamp files, and `remote-settings.json`. So it fetched remote policy and settings. It also wrote a transcript JSONL under `projects/`, which is a second trace store holding file contents. None of it is committed.
- **Cost is reported twice, and the two figures differ.** The CLI's `total_cost_usd` came out about 10 % below `runtime.pricing` on the same usage. `max_budget_usd` is checked against the CLI's own estimate between turns. `runtime.pricing` remains the source of truth.
- **Timeouts.** A `Bash` timeout in the CLI kills the `docker` client but not the container. `sandbox.py` names each container and kills it by name, and `test_sandbox_kills_the_container_on_timeout` covers that.
- **A module name shadowed a package.** This directory first had a `tools.py`, which shadowed the top-level `tools` package and failed with `ImportError: cannot import name 'ALLOWED_PROGRAMS' from partially initialized module 'prototypes.fixer.sandbox' (most likely due to a circular import)`. It is now `fixer_tools.py`. The repository has no `__init__.py` files, so a prototype module must not reuse a top-level package name.
- **Recorder timing.** The first recorder stamped each `PostToolUse` duration on the most recent call, which is wrong when calls overlap. It now matches calls by `tool_use_id`. The `bash_seconds` in the four recorded runs above predate that fix, so they are left out of the table.
