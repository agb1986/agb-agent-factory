# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The release procedure is in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Fixed

- CI runs on ubuntu-24.04 instead of ubuntu-latest, whose move to ubuntu-26.04 made the image key scan overrun the job timeout.

### Added

- `sentry.list_events` and `sentry.read_issue`, read-only tools that return a Sentry project's recent events and an issue's stack trace, release tag, breadcrumbs and counts as compact JSON; they read `SENTRY_AUTH_TOKEN`, `SENTRY_ORG` and `SENTRY_PROJECT` from the environment and are never given to a sandboxed agent (#107).
- Read-only `memory.search` tool over `agb mem search`, with a result limit and a size cap, never for sandboxed agents (#89).
- Documented conventions for manifests, agent layout, output, gates, the trace row and secrets (#1).
- Package scaffold with uv, ruff, ty, pytest and pre-commit (#2).
- Read-only `git.read_file`, `git.show_diff` and `rg.search` tools, with path validation (#3).
- Registry resolver with `name@^MAJOR.MINOR` ranges, semver resolution and manifest validation (#4).
- LLM runtime on the Anthropic SDK tool runner, with structured outputs (#5).
- Trace store in DuckDB, recording each run's kind, cost and decision, with a pricing table (#6).
- `code-reviewer@1.2.0`, run end to end against the live API (#7).
- Eval runner, `factory eval`, with an LLM-judge grader and a pass-rate gate (#8).
- Fixture repositories with planted defects, built by `fixtures/build.sh` (#9).
- Publish gate, `factory publish-check`, which requires a version bump when a prompt, schema or question changes (#10).
- `judge@2.0.0`, which applies its weights to the four scores in code (#11).
- `security-reviewer@1.0.0` (#12).
- `perf-reviewer@0.3.0` (#13).
- `review_panel` pipeline and `factory review`: parallel reviewers, a judge, JSON output and exit codes (#14).
- Jev SDK spike, recording the Python access path, the model id and the limits (#15).
- `kind: jev` agent manifests, with question validation and generated output models (#16).
- Jev runner with rendering, truncation, batched requests, timeouts and retries (#17).
- Gate decisions in `runtime/gates.py`, and diff-triage in route-only mode in the review panel (#18).
- `judge-gate` agent: a Jev call that decides whether the judge can be skipped, also used as a cheap eval grader (#19).
- Labelled set of 126 merged pull requests for calibrating the diff-triage skip threshold (#20).
- `scripts/calibrate.py`, which sweeps the diff-triage skip threshold over the labelled diffs (#20).
- `scripts/shadow_report.py`, which reports the diffs the diff-triage skip would have skipped (#20).
- `docs/jev-calibration.md`, the calibration method, its results and the shadow period (#20).
- `diff-triage@0.2.0`, with the calibrated `low_risk` skip gate at 0.5. Triage still runs route-only, so nothing is skipped yet (#20).
- Decision on the fixer runtime, recorded in ADR 0001 (#21).
- `test-writer@0.1.0` and `fixer@0.1.0` agents, run on the sandbox tools (#22).
- Action guard, a per-tool-call check that decides whether a tool call that is not read-only may run (#23).
- `factory fix`: a fix loop that runs in a sandbox over a throwaway clone and writes a reviewed patch (#24).
- Retries, a token limit per run and a cost guard for each pipeline (#25).
- Async runtime on `AsyncAnthropic`, with the reviewers run concurrently (#26).
- Continuous integration for lint, type checks and tests, with a coverage floor (#27).
- Docker image for the deployment, with a data volume, secrets from an env file and an image key scan (#28).
- Eval cases for `diff-triage@0.2.1`, with `$fixture` payloads, expected answers and gate decisions, and the `recall_risky`, `skip_rate` and `escalation_rate` metrics, gated on `evals.min_recall_risky` (#66).
- `fixer@0.1.1` and `test-writer@0.1.1` reach four and five eval cases, with two new fixtures, `mixed-red` and `sql-injection-red` (#65).
- `factory cost --by kind`, which splits spend between `claude` and `jev` runs, and `scripts/triage_saving.py`, which measures the diff-triage saving against `factory review --no-triage` (#67).
- `parent_run_id` column in the trace store, and `factory trace`, which exports a pipeline run as an Agent Flow event log with the real run tree (#71).
- Live tests are opt-in: `live` and `live_jev` tests are deselected unless `FACTORY_LIVE=1` is set, in addition to the key. One shared `FACTORY_TRACE_DB` is documented for orchestrated workers (#72).
- `unless` on a gate, which keeps it from firing when the gate it names fires. `diff-triage@0.3.0` gives `unsure` `unless: low_risk`, so a low-risk diff is skipped whatever the routing confidence, and its `reviewers` question names what `code-perf` and `code-security` are for (#75).
- `action-guard@0.1.1` no longer refuses a write inside a worktree that sits under `/tmp`, `/home` or another listed prefix; the outside test applies to the path the call names, and its eval gains a case for it. `test-writer@0.1.2` tests only the behaviour the finding names and calls the code only as the existing tests do, so its test passes on the code before the change. Fixer and test-writer meet their eval floors (#75).
- Read-only `web.fetch` and `web.search` tools that wrap `agb`, with a per-agent domain allow-list (`tools.web.allow_domains`) enforced by the runner, a per-process fetch cache, untrusted-content data blocks, and no access from a sandboxed agent (#102).
- `deps.graph` and `deps.impact`, read-only tools that return the import graph of a Python, TypeScript or JavaScript repository and the modules that import some files, with their depth, and `factory deps <repo> [--impact PATH ...] [--json]`. Dynamic imports, path aliases and other imports that name no single file are reported as unresolved, never guessed (#88).

<!-- Until the first tag, the Unreleased link points at the commits on main. At the first tag, docs/releasing.md moves these entries under the new version and changes this link to compare the tag with HEAD. -->
[Unreleased]: https://github.com/agb1986/agb-agent-factory/commits/main
