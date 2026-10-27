# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The release procedure is in [docs/releasing.md](docs/releasing.md).

## [Unreleased]

### Fixed

- CI runs on ubuntu-24.04 instead of ubuntu-latest, whose move to ubuntu-26.04 made the image key scan overrun the job timeout.

### Added

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

<!-- Until the first tag, the Unreleased link points at the commits on main. At the first tag, docs/releasing.md moves these entries under the new version and changes this link to compare the tag with HEAD. -->
[Unreleased]: https://github.com/agb1986/agb-agent-factory/commits/main
