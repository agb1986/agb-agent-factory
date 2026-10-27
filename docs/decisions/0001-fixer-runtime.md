# ADR 0001: Fixer runtime

Status: accepted, 2026-10-25. Issue: #21. Evidence: [prototypes/fixer/](../../prototypes/fixer/README.md).

Implemented, 2026-10-27: #22 added the agents and the sandbox-only tools, #23 the action guard, and #24 the fix loop (`pipelines/fix_loop.py`, `pipelines/sandbox.py`). #24 also labels each sandbox container with the work tree it mounts, so `tools.sandbox.kill_sandboxes` can stop any left running when a run ends, and reads the image name from `FACTORY_SANDBOX_IMAGE`.

This record answers the open question in [PLAN.md](../PLAN.md): whether the fixer runs on the Claude Agent SDK, on Managed Agents, or on the factory's own runtime with a hand-built Docker sandbox. Every fact below was checked on 2026-10-25, either by a live run of the prototype on this host or against the source linked in its section. Figures from a live run say so; the rest come from documentation.

## Decision in one line

The fixer and the test writer run on option (a): the existing Tool Runner runtime (`runtime.runner.run`, #5), with a write tool and a shell tool whose commands execute in a fresh, networkless Docker container per call, and the action guard of #23 as a `ToolHook`.

## Context

The review panel only reads code. The fix loop (#24) is the first part of the factory that changes it: test-writer@0.1 and fixer@0.1 (#22) write files and run tests in a throwaway work tree, behind an action guard (#23), and the result has to deploy as a one-shot container on the CasaOS host (#28). [CONVENTIONS.md](../CONVENTIONS.md#tools) already says that a tool that writes may be allowed only for an agent that runs inside the sandbox, behind the guard.

The runtime this decision has to fit:

- `runtime.runner.run(spec, payload, trace, *, repo_path, pipeline_run_id, hook, client, tag)` drives `client.beta.messages.tool_runner` from `anthropic` 2.3.0 over the registry's tools, with adaptive thinking, `output_config.effort` and `output_config.format` from the manifest, and writes one costed `TraceRow`.
- `runtime/hooks.py` defines `ToolHook.before_tool(call) -> ToolDecision` and `after_tool(call, result)`. A `ToolDecision.deny(message)` goes back to the model as an error tool result, and a `FactoryError` raised by a hook ends the run unchanged.
- The SDK's synchronous runner executes the tool calls of one assistant turn one after another, in the order the model gave them (`_generate_tool_call_response` in `anthropic/lib/tools/_beta_runner.py`, 2.3.0), and the factory does not turn on eager tool execution.
- `registry/spec.py` caps a run with `max_tool_turns`, `timeout_s` and `max_output_tokens`. There is no cap on total tokens yet.

The task used to compare the options is the `off-by-one` fixture from #9 with its one planted defect, `last-window-dropped`, given to the fixer as a finding. A fix counts only if the defect's own example and the fixture's test suite both pass in the sandbox afterwards and this repository's `git status` is unchanged.

## Options considered

### (a) Tool Runner runtime with a hand-built Docker sandbox

Checked 2026-10-25 by live runs. Sources: [prototypes/fixer/run.py](../../prototypes/fixer/run.py), [sandbox.py](../../prototypes/fixer/sandbox.py), [fixer_tools.py](../../prototypes/fixer/fixer_tools.py), [Tool use concepts](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview).

The prototype registers four tools: `fs.list_files`, `fs.read_file`, `fs.write_file` and `shell.run`. The write tool resolves every path against the work tree and refuses anything outside it, anything under `.git`, a symlink that leads out, and content over 256 KiB. The shell tool accepts only `python` and `pytest` and runs each command as

```text
docker run --rm --name <unique> --network none --cpus 1 --memory 512m --pids-limit 128 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --user <uid>:<gid> \
  --volume <worktree>:/work --volume <worktree>/.git:/work/.git:ro --workdir /work \
  factory-fixer-sandbox:proto <argv>
```

On timeout it kills the container by name, because killing the `docker` client leaves the container running. The agent is `fixer-proto@0.0.1` on `claude-sonnet-5-5` at effort `xhigh`, and it runs through the unchanged `runtime.runner.run` with a recording `ToolHook` in the place the action guard will take.

Three live runs, all fixed:

| Run | agent s | wall s | cost USD | input | output | cache read | cache write | tool calls |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 16.3 | 19.40 | 0.0266 | 4,957 | 1,184 | 5,832 | 1,458 | 7 |
| 2 | 13.0 | 15.63 | 0.0231 | 4,957 | 1,170 | 7,290 | 0 | 7 |
| 3 | 13.7 | 16.33 | 0.0231 | 4,957 | 1,168 | 7,290 | 0 | 7 |

Every run made the same seven calls with no tool errors: it listed and read the files, wrote a regression test first and the fix second, and ran the tests twice in the sandbox. The patch changes `range(window, len(values) - 1)` to `range(window, len(values))` and adds `test_includes_last_window`. Peak RSS was 162 MB. `test_option_a_fixes_the_fixture` passed live afterwards.

### (b) Claude Agent SDK

Checked 2026-10-25 by live runs. Sources: [prototypes/fixer/sdk_run.py](../../prototypes/fixer/sdk_run.py), [Agent SDK for Python](https://code.claude.com/docs/en/agent-sdk/python), [Agent SDK hooks](https://code.claude.com/docs/en/agent-sdk/hooks), [PreToolUse decision control](https://code.claude.com/docs/en/hooks.md#pretooluse-decision-control), [Sandboxing](https://code.claude.com/docs/en/sandboxing.md) and its [security limitations](https://code.claude.com/docs/en/sandboxing.md#security-limitations), [sandbox settings](https://code.claude.com/docs/en/settings-reference#sandbox-excludedcommands), [Secure deployment](https://code.claude.com/docs/en/agent-sdk/secure-deployment.md).

`claude-agent-sdk` 0.2.174 drives the Claude Code CLI 2.1.310 it bundles, with the CLI's `Read`, `Write`, `Edit` and `Bash` tools. The CLI's own Bash sandbox could not be used: asked to start with it required (`enabled`, `failIfUnavailable`, `allowUnsandboxedCommands: false`), the CLI refused with `Sandbox required but unavailable: ... socat not installed`. This host has `bwrap` but not `socat`, and installing host packages is itself a cost this record weighs. The prototype therefore puts the isolation in a `PreToolUse` hook: it denies every other tool, confines file paths to the work tree, and rewrites each allowed `Bash` command into the same `docker run` as option (a). The CLI gets an empty `CLAUDE_CONFIG_DIR`, no setting sources and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.

Four live runs; all four fixed the code:

| Run | agent s | wall s | turns | cost USD | CLI estimate USD | cache read | denied by the guard |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 33.1 | 35.33 | 14 | 0.0768 | 0.0691 | 76,997 | `find`, `ls -R`, `git ls-files`, `StructuredOutput` ×2 |
| 2 | 20.0 | 22.14 | 11 | 0.0362 | 0.0318 | 44,052 | `find`, `git ls-files` |
| 3 | 17.0 | 19.12 | 10 | 0.0332 | 0.0300 | 31,375 | `ls -R` on an absolute host path |
| 4 | 21.9 | 23.99 | 12 | 0.0391 | 0.0335 | 56,505 | `find`, `git ls-files` |

What the runs showed:

- Run 1 returned no report. The guard denied the CLI's hidden `StructuredOutput` tool, which carries `output_format`, and the CLI still ended with `subtype: success` and `is_error: false`. The fix was to allow that tool by name and validate the output in the runner.
- In every run the model first reached for `find`, `ls -R` or `git ls-files`, was denied, and fell back to listing files with `python -c "import os; ... os.walk(...)"`, which an allow-list of programs cannot tell from any other Python. In run 3 it used the work tree's absolute host path, which the CLI shows it as its `cwd` but which does not exist in the container.
- A `Write`/`Edit` path rule never sees a file written by a `Bash` command, so the guard has to understand every Bash string as well. The CLI's sandbox documentation says the same of its own sandbox: `allowUnsandboxedCommands` defaults to true, without `failIfUnavailable` it falls back to running unsandboxed, and its network rules do not cover `WebFetch`.
- With an empty config directory and non-essential traffic off, the CLI still wrote `.claude.json`, `policy-limits.json` and `remote-settings.json`, so it fetched remote policy and settings, and it wrote a transcript JSONL holding file contents.
- The CLI's `total_cost_usd` came out about 10 % below `runtime.pricing` on the same usage, and `max_budget_usd` is checked against that estimate between turns.
- A `Bash` timeout in the CLI kills the `docker` client but not the container.

Peak RSS was 245 to 254 MB. `test_option_b_fixes_the_fixture` passed live afterwards.

### (c) Managed Agents

Checked 2026-10-25 against documentation only; no prototype. Sources: [Managed Agents overview](https://platform.claude.com/docs/en/managed-agents/overview.md), [Environments](https://platform.claude.com/docs/en/managed-agents/environments.md), [Self-hosted sandboxes](https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes.md), [Sessions](https://platform.claude.com/docs/en/managed-agents/sessions.md).

Managed Agents runs the agent loop at Anthropic and executes tools in a per-session container, behind the beta header `managed-agents-2026-04-01`. The facts that matter here:

- Repository input is either read-only file mounts, where the agent writes modified copies to new paths under the working directory `/workspace`, or a `github_repository` resource that needs an `authorization_token` (a GitHub personal access token) used by an Anthropic-side git proxy. Outputs are written to `/mnt/session/outputs/` and become listable through the Files API after 1 to 3 s of indexing.
- Networking is `unrestricted` or `limited`, which denies by default and takes an `allowed_hosts` list.
- A guard can be built from `permission_policy: {type: "always_ask"}`: the session emits a confirmation request for each built-in tool call and waits for a `user.tool_confirmation` reply. Custom tools are not governed by permission policies.
- Usage arrives as `session.usage` and `span.model_request_start` and `span.model_request_end` events.
- The list price is model tokens, plus $10 per 1,000 web searches, plus $0.08 per hour of session running time.
- With a self-hosted sandbox (`config: {type: "self_hosted"}`) tools execute on our machine, but the loop stays at Anthropic, and tool inputs and outputs still flow to Anthropic's control plane. `web_search` and `web_fetch` always run on Anthropic's servers.
- Managed Agents is in beta on the first-party API and on Claude Platform on AWS, and is not offered on Bedrock or Vertex AI.

No prototype was built. Its documented shape already fails two requirements of #24 that the prototypes of (a) and (b) meet: the work tree is not the writable mount (the agent writes copies, or works on a clone pulled through a token), and the repository's code and every tool result leave the host even when the sandbox is ours. A prototype would also have needed a GitHub token or a file upload per run and beta access, for a result that could not change the decision.

## Criteria

Each cell is one sentence of evidence. Measured figures are from the live runs above, on the `off-by-one` fixture with `claude-sonnet-5-5` at effort `xhigh`; (c) was not measured.

| Criterion | (a) Tool Runner and Docker | (b) Claude Agent SDK | (c) Managed Agents |
| --- | --- | --- | --- |
| Isolation: writes outside the work tree | `fs.write_file` refuses any path outside the work tree, under `.git` or through a symlink out, the container's root is read-only with `.git` mounted read-only, and `test_fixer_sandbox.py` covers each escape. | Only the hand-written `PreToolUse` guard stands between the CLI's `Write`, `Edit` and `Bash` and the host, because the CLI's sandbox would not start without `socat`, and the model sent an absolute host path through `Bash` in run 3. | The agent never touches our work tree; it writes copies in an Anthropic container, so a patch has to be rebuilt from `/mnt/session/outputs/`. |
| Isolation: network | Every command runs in `docker run --network none`, and `test_fixer_sandbox.py` shows a socket connect failing inside it. | The CLI's network sandbox needs `socat`, does not cover `WebFetch`, and falls back to unsandboxed without `failIfUnavailable`, so the prototype had to deny every tool but four and use option (a)'s container. | `limited` networking denies by default with an `allowed_hosts` list, but tool inputs and outputs still cross to Anthropic's control plane, even with a self-hosted sandbox. |
| Cost per fix (off-by-one) | $0.0231 to $0.0266 per fix, mean $0.024 over three runs, with seven tool calls and 7,290 cache-read tokens each. | $0.0332 to $0.0768, mean $0.046 over four runs, with 31,000 to 77,000 cache-read tokens a run: 1.4 to 1.7 times option (a) when warm and 2.9 times on the first run. | Not measured; tokens are billed as usual plus $0.08 per session-hour (about $0.0005 for 20 s), and the token count depends on Anthropic's agent harness, which we do not control. |
| Latency | 13.0 to 16.3 s for the fixer and 15.6 to 19.4 s for the whole run, the two sandboxed checks included. | 17.0 to 33.1 s for the fixer and 19.1 to 35.3 s in all, over 10 to 14 turns, several of them spent on denied shell listings. | Not measured; it adds session and container creation, an event stream, and 1 to 3 s before outputs can be listed. |
| Where the action guard (#23) attaches | `ToolHook.before_tool` already sees every call's dotted name and arguments before it runs, a deny reaches the model as an error tool result, and a `FactoryError` from the hook ends the run. | A `PreToolUse` callback that sees the CLI's own tool names and raw Bash strings, has to allow the hidden `StructuredOutput` tool, and cannot see a file written through `Bash`. | `permission_policy: always_ask` with a `user.tool_confirmation` reply per built-in tool call, with custom tools outside the policy. |
| Tracing with cost into `runtime/trace.py` | `runtime.runner.run` writes the costed `TraceRow` itself, with the run's `pipeline_run_id` and `tag`, so nothing new is needed. | Nothing is written for us: the prototype builds a `TraceRow` by hand from the result message, the CLI's own cost figure is about 10 % lower, and the CLI keeps a second transcript store holding file contents. | A new adapter would turn `session.usage` and `span.model_request_*` events into a `TraceRow`, and the transcript also stays with Anthropic. |
| Operational burden on the CasaOS host (#28) | Docker, which #28 needs anyway, one image built from a two-instruction Dockerfile, no host packages, and 162 MB peak RSS. | The bundled CLI pinned to the SDK version, `socat` and `bubblewrap` for its sandbox, a writable config directory per run, remote policy and settings fetches, and 245 to 254 MB peak RSS. | No local sandbox, but a beta API, a GitHub token or file upload per run, and every fix stops when the service or the beta changes. |
| Vendor lock-in | The Messages API through the `anthropic` Tool Runner that every agent already uses, with our own tools and a plain Docker sandbox. | The fixer's tools, prompts and hook payloads become those of the Claude Code CLI, and change with its releases. | The loop, the tool traffic and the agent definition live at Anthropic under a beta header, with no Bedrock or Vertex AI support. |

## Decision

Use option (a). It fixed the fixture in all three runs at the lowest cost and latency, through the runtime the factory already has, and every criterion is met by code this repository owns and tests. The guard and the trace need no new mechanism.

Option (b) also fixed the code, but only after the prototype replaced the CLI's isolation with option (a)'s container and a guard that has to know the CLI's internal tools; what is left of (b) is a second agent loop with a second cost estimate and a second transcript store. Option (c) was ruled out on its documentation: our code and tool results leave the host, the work tree is not the writable mount, and it is beta.

## Consequences

### What #22 to #24 and #28 must build

- **#22, test-writer@0.1 and fixer@0.1.** Both are `kind: llm` agents run by `runtime.runner.run`. Move `fs.write_file` and `shell.run` into `tools/` as sandbox-only tools: registered only for an agent that runs inside the sandbox, confined to the work tree, never `.git`, with a size cap on writes and an allow-list of programs. Keep `fs.list_files`, so the model does not reach for shell listings. The output model lists the files changed and the commands run. Add a cap on total tokens to `LimitsSpec` and the runner, because the manifest has only `max_tool_turns`, `timeout_s` and `max_output_tokens`. Keep the synchronous Tool Runner without eager tool execution, so the tool calls of one turn run in order; in both prototypes the model asked for several calls in one turn, and one may write a file the next one tests.
- **#23, action-guard.** Implement it as a `ToolHook`. `before_tool` judges every call not in the read-only set and returns `ToolDecision.deny(message)` to refuse it, which the model receives as an error tool result. On the third denial it raises a `FactoryError`, which `runtime.runner.run` lets through unchanged and which ends the run. It traces every decision. The acceptance test can drive it with the same `fs.write_file` and `shell.run` calls as `test_fixer_sandbox.py`.
- **#24, fix_loop.** Run every command with the `docker run` flags above: no network, a read-only root, CPU, memory and pids caps, no capabilities, the host uid, the work tree as the only writable mount and `.git` mounted again read-only. Name each container and kill it by name on timeout. Build the patch on the host with `git diff --no-ext-diff --no-textconv` and fsmonitor off, so no program the target repository configures runs outside the sandbox. The sandbox has no network, so the image must already hold the target repository's test dependencies.
- **#28, deployment.** The fix loop starts a container per command, so the factory's own container needs a way to start sibling containers. Mounting the Docker socket gives that container root-equivalent control of the host, and bind mounts are resolved on the host, so the work tree must live at the same path inside and outside the factory container. #28 decided in [deploy.md](../deploy.md#sandbox-containers): the factory image has no Docker CLI and the container has no socket until the fix loop needs them, and then it uses sibling containers through the host's socket (build argument `WITH_DOCKER_CLI=1`, a commented-out socket mount in `compose.yaml`, the work tree at the same path inside and outside) with the root-equivalent risk stated. Nothing in this record installs host packages, and the image does not install `socat`: option (b) is ruled out, so its sandbox is not built.

### What the decision rules out

- The Claude Agent SDK and the Claude Code CLI as the fixer's runtime, their built-in tools, and their Bash sandbox. `claude-agent-sdk` is not a project dependency; the prototype runs it only through `uv run --with`.
- Managed Agents for the fixer while it is beta and while tool traffic leaves the host. Revisit it if a self-hosted sandbox keeps tool inputs and outputs local and the API leaves beta.
- `socat`, `bubblewrap` or any other host package for sandboxing, and any model-written command running on the host.
- Network access from the sandbox, including `pip install` during a fix.

### Prototype

`prototypes/fixer/` is throwaway evidence, not production code. #22 to #24 may copy from it, and it can be deleted once they have landed. Its [README](../../prototypes/fixer/README.md) has the commands to rerun every result above.
