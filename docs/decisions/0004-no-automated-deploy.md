# ADR 0004: No automated deploy

Status: Proposed, 2026-11-20. Issue: #109.

## Decision in one line

`factory hotfix` stops at a pull request labelled `hotfix`; a person merges and deploys it, and no factory pipeline merges or deploys anything until the risk gate has a measured recall on real incidents at or above the triage skip gate's bar over a shadow period of the same length.

## Context

The hotfix pipeline (#109) takes a reproduced incident ticket (#108) through the fixer, the sandboxed suite and the review panel, then opens a pull request through `runtime.outside.write("github.create_pr", ...)` under ADR 0003. The next step would be to merge and deploy. Doing that without a person depends on the risk gate (diff triage, the judge gate and the judge's score) never letting a harmful fix through, and that has not been measured on real incidents. The gate has only been calibrated on labelled diffs.

ADR 0003 already lists no deploy tool among the permitted outside writes (its "Deployment" section). This record states when that may change and on what evidence.

## Options considered

### (a) Stop at a pull request (chosen)

The pipeline opens the pull request and moves the ticket to `in_review`. There is no merge call and no deploy tool. The time from the event to the pull request is recorded in the hotfix report, so the cost of the human step can be measured.

### (b) Auto-merge when the panel ships

Rejected: the panel's ship verdict on a hotfix has no measured miss rate on real incidents.

### (c) Auto-deploy behind a feature flag or canary

Rejected for the same reason, and because the factory has no deploy target or rollback contract to call.

## Decision

No automated merge or deploy until all of these hold:

- **Metric.** Recall of the risk gate on real incidents: of the hotfix pull requests whose fix a person later judged harmful (reverted, or changed before merge because it was wrong), the share that the risk gate flagged (triage routed it to the full panel, the judge gate escalated, or the judge did not ship).
- **Bar.** At or above the bar of the triage skip gate in #20: `evals.min_recall_risky: 0.98` in `agents/diff-triage/agent.yaml`. docs/jev-calibration.md ("Method") requires the calibrated threshold to reach the manifest's `evals.min_recall_risky`.
- **Period.** A shadow period of the same length as the triage gate's: 14 days. docs/jev-calibration.md ("Shadow mode") says the 14-day clock starts on the day the calibrated gate is merged and published, and `scripts/shadow_report.py` defaults to the 14 days ending today.
- **Data.** Every hotfix pull request opened in the window, each labelled after the fact as harmful or not by a person, joined to its pipeline run's triage, judge gate and judge rows in the trace store. With few harmful fixes the recall is coarse (in the triage calibration, 22 risky diffs meant one miss dropped recall to about 0.955), so the window must hold enough harmful labels for one miss to show below the bar.

This decision would change on that data: a 14-day shadow window in which the measured recall is at or above 0.98. A later record would then decide the merge and deploy tools, their agents and their dry-run behaviour under ADR 0003.

## Consequences

- `factory hotfix` has no `--merge` or `--deploy` option, and `tools.registry.OUTSIDE_WRITES` gains no merge or deploy tool.
- The `hotfix` label must exist on the target repository before a run; `github.create_pr` refuses a missing label and the factory never creates one.
- The hotfix report records `event_time`, `pr_opened_at` and `time_to_pr_seconds`, so the delay a person adds after the pull request can be measured against it later.
- Labelling hotfix outcomes as harmful or not is operator work and is not built here.
