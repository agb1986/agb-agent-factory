# Diff-triage calibration

How the diff-triage skip threshold is calibrated (#20): the labelled diffs, the sweep that picks the threshold, how the result goes into the manifest, and the shadow period before anything is skipped. Every fact below was checked on 2026-10-27 against the code and files linked in its section.

The live steps ran on 2026-10-27: the panel labelling pass over the `needs_panel` rows, the live model-id check and the real sweep. [Results](#results) records the sweep. The sweep ran again on 2026-10-30 for diff-triage@0.3.0, see [the `unsure` gate yields to `low_risk`](#the-unsure-gate-yields-to-low_risk-030). [Shadow results](#shadow-results) records the shadow period, which started on 2026-10-30.

## Dataset

Checked 2026-10-27. Sources: [evals/calibration/labels.yaml](../evals/calibration/labels.yaml), its schema in [evals/calibration/labels.py](../evals/calibration/labels.py).

The set has 126 merged pull requests, all in repositories owned by github.com/agb1986. A row stores the repository URL and a commit pair, never source: `base` is the first parent of the merge commit and `head` is the merge commit, so the diff is exactly what the pull request merged. [evals/calibration/fetch.py](../evals/calibration/fetch.py) fetches it again at run time through `gh api .../compare/{base}...{head}`, so `gh` must hold a token that can read the 94 private rows.

A diff is risky when its worst finding would be major or blocker on the reviewers' scale ([agents/code-reviewer/system.md](../agents/code-reviewer/system.md)), and safe otherwise. Each row has a one-line reason.

| Count | Value |
| --- | --- |
| Diffs | 126: 104 safe, 22 risky |
| Worst finding | none 54, minor 50, major 20, blocker 2 |
| Private | 94 |
| Label source | `reader` 98, `panel` 28 |
| Panel labels | 28: 8 risky, 20 safe |
| Labelling spend | $9.10 of the $15 budget |

| Repository | Safe | Risky | Private |
| --- | --- | --- | --- |
| agb-agent-factory | 23 | 0 | 23 |
| agb-tools | 10 | 3 | 13 |
| agb-trader | 18 | 3 | 21 |
| ai-data-store | 4 | 3 | 0 |
| cannabot | 13 | 5 | 18 |
| finance-agent | 3 | 0 | 0 |
| torrent-agent | 15 | 7 | 0 |
| xelpfin | 18 | 1 | 19 |

A reader label means someone read the diff and judged whether a careful reviewer would raise a major or blocker finding. Docs-only, test-only and dependency-bump diffs were labelled from their file lists and contents; the rest were read in full. The reader marked 28 rows `needs_panel`, and where the doubt was whether a major finding exists at all, labelled the row risky at major, so doubt could only make the measured recall stricter: 21 of the 28 were risky.

On 2026-10-27 each of the 28 went through a full review panel (judge@2.0.0 over code-reviewer@1.2.0, security-reviewer@1.0.0 and perf-reviewer@0.3.0), and the worst finding the judge kept became the label, with `source: panel` and `needs_panel` cleared. The panel kept a major finding in 8 of the 21 risky rows and found minor or nothing in the other 13; the 7 safe rows stayed safe. That is why the set moved from 35 risky to 22. The panel runs cost $9.10, a mean of $0.29 a run, including one first attempt that stopped when code-reviewer hit its turn limit and was run again.

Fetched diffs are untrusted data. They are handed to the triage agent as text and never run, and neither script prints a diff, a file name or a finding's text.

### Truncation

Checked 2026-10-27 with `uv run python scripts/calibrate.py --dry-run`, which fetches the diffs and measures them without a Jev call, and again by the real sweep the same day.

The manifest's `input.max_chars` is 24000. Rendered sizes have a median of 5948 characters, a 90th percentile of 19431 and a maximum of 649532. Five of the 126 diffs exceed `max_chars`, four safe and one risky. A truncated input is always reviewed (`runtime.gates.decide`), so no threshold can skip more than 100 of the 104 safe diffs: 96.2%.

## Method

Sources: [scripts/calibrate.py](../scripts/calibrate.py), [evals/calibration/sweep.py](../evals/calibration/sweep.py), [runtime/gates.py](../runtime/gates.py).

`scripts/calibrate.py` fetches every labelled diff and runs diff-triage over each one once, through the review panel's `run_triage`. The calls share one `Budget` (`--budget-usd`, default $1.00). Each call is traced under the pipeline run id `calibrate-<run>-<label id>`, and its cost is read back by that id.

The sweep then decides every diff again at each candidate threshold with `runtime.gates.decide`, on a copy of the manifest whose `low_risk` skip gate (`needs_review` `p_below` the threshold) is set by `runtime.gates.with_skip_threshold`. The numbers are what the panel would do: an escalating gate still clears the skip unless its `unless` names the skip gate, as `unsure` does since diff-triage@0.3.0, and a truncated input is always reviewed. The default thresholds are 0.02, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4 and 0.5; `--thresholds` replaces them.

At each threshold the sweep reports:

| Column | Meaning |
| --- | --- |
| Recall on risky diffs | Risky diffs still reviewed, over all risky diffs |
| Skip rate on safe diffs | Safe diffs skipped, over all safe diffs |
| Cost per diff | Mean Jev cost, plus the share of diffs still reviewed times `--panel-cost-usd` when given |
| Missed | The risky diffs this threshold would skip |

The chosen threshold is the one that skips the most safe diffs among those whose recall reaches the manifest's `evals.min_recall_risky` (0.98). A tie goes to the lower threshold, which leaves more margin. A diff whose Jev call failed is left out of every number, listed, and makes the script exit 1, so an incomplete sweep is never mistaken for a result. A rejected key stops the run at the first call with exit 4.

Run it from the repository root:

```sh
set -a; source ~/.config/agb/secrets.env; set +a
uv run python scripts/calibrate.py --panel-cost-usd 0.29 --json --trace /tmp/calibrate.duckdb
```

`--panel-cost-usd` is the cost of one full panel review; take it from recent panel runs with `factory cost --since <date> --by pipeline`. The 2026-10-27 sweep used $0.29, the mean of the 28 labelling runs. Without it, the cost column is the Jev cost alone.

Point `--trace` at a scratch file and delete it afterwards. The Jev runner records its full payload, the diff, as `input_json` in the trace store, so a run against the default store copies every labelled diff, private ones included, into `data/traces.duckdb`. Some labelled diffs contain credentials committed in their own repositories. The sweep sends each diff to TypeSafe and the trace store keeps it, so a run copies those credentials to both. The 2026-10-27 sweep sent them to TypeSafe, and its scratch trace store was deleted afterwards. Rotating a credential is up to the repository that committed it and is not tracked here.

At jev-1.14.0 prices, $0.042 per million input tokens with output free ([runtime/pricing.py](../runtime/pricing.py)), and with input cut at 24000 characters, one call is at most about 6000 tokens, or about $0.00025. The 2026-10-27 sweep cost $0.0155 for the 126 calls, $0.00012 a diff.

Exit codes: 0 a threshold reaches the recall, 1 none does or a call failed, 2 bad arguments, 3 no labels file, 4 a missing or rejected key, or `gh` cannot read a repository.

## Results

Run on 2026-10-27 with the command in [Method](#method), against diff-triage@0.1.0 with no skip gate yet. Every response carried the manifest's model id, `jev-1.14.0`. 126 diffs measured, 5 truncated, none failed; Jev cost $0.0155 in all.

| Threshold | Recall on risky diffs | Skip rate on safe diffs | Cost per diff | Missed |
| --- | --- | --- | --- | --- |
| 0.02 | 1.000 (22/22) | 0.000 (0/104) | $0.2901 | none |
| 0.05 | 1.000 (22/22) | 0.000 (0/104) | $0.2901 | none |
| 0.1 | 1.000 (22/22) | 0.000 (0/104) | $0.2901 | none |
| 0.15 | 1.000 (22/22) | 0.000 (0/104) | $0.2901 | none |
| 0.2 | 1.000 (22/22) | 0.000 (0/104) | $0.2901 | none |
| 0.25 | 1.000 (22/22) | 0.010 (1/104) | $0.2878 | none |
| 0.3 | 1.000 (22/22) | 0.019 (2/104) | $0.2855 | none |
| 0.4 | 1.000 (22/22) | 0.067 (7/104) | $0.2740 | none |
| **0.5** | 1.000 (22/22) | 0.077 (8/104) | $0.2717 | none |

The cost column includes the $0.29 panel for every diff still reviewed. No threshold skipped a risky diff, so every row reaches the 0.98 recall and the chosen threshold is 0.5, the one that skips the most safe diffs.

0.5 is the top of the default grid, so this sweep cannot say whether a higher threshold would also reach the recall. The lowest `needs_review` probability on a risky diff was 0.66, which leaves a margin of 0.16 above the threshold. With 22 risky diffs a single miss drops recall to 0.955, below the target, so the set is too small to spend that margin.

The skip rate is held down by the `unsure` gate rather than by the threshold. That gate escalates when the `reviewers` confidence is below 0.6, and it escalated 75 of the 126 diffs, 59 of them safe. Of the 20 safe diffs with `needs_review` below 0.5, 12 were escalated and none was truncated, which leaves the 8 that skip. The sweep moves only the skip gate, so the `unsure` value is not calibrated here.

### The `unsure` gate yields to `low_risk` (0.3.0)

On 2026-10-30 `factory publish-check diff-triage@^0.2` failed its 0.9 pass-rate floor at 5 of 7 (#75): the `docs-only` eval case had `needs_review` 0.26 and `reviewers` confidence 0.41, so `unsure` escalated and cleared the skip the case expects. The routing confidence says nothing about a diff nobody is going to review, so 0.3.0 gives `unsure` the field `unless: low_risk` (docs/CONVENTIONS.md, "Gates"): it does not fire when the skip gate fires. The skip gate's threshold and the recall it was calibrated for are unchanged; what changes is that the safe diffs `unsure` was pulling back get skipped. The same release gives the `reviewers` question text a cue for `code-perf` and `code-security`, because the `n-plus-one` case was routed to `code` alone.

The sweep was run again on 2026-10-30 against the 0.3.0 manifest with the same command and `--panel-cost-usd 0.29`: 126 diffs measured, 5 truncated, none failed, Jev cost $0.0155, model id `jev-1.14.0`. Recall stays 1.000 at every threshold and the chosen threshold stays 0.5. The skip rate on safe diffs at 0.5 is now 0.183 (19/104) against 0.077 (8/104) before, and the cost per diff $0.2464 against $0.2717; at 0.4 it is 0.125 (13/104), at 0.3 and 0.25 0.019 (2/104), and 0 below that. The new question text moved a few probabilities, which is why the count is 19 and not the 20 the 2026-10-27 run implied. `factory eval diff-triage@^0.3` passed 7 of 7 the same day, recall on risky cases 1.0, skip rate 0.43 over the 7 cases.

## Writing the manifest snippet

When a threshold reaches the recall, `calibrate.py` prints the gate to paste under `gates:` in [agents/diff-triage/agent.yaml](../agents/diff-triage/agent.yaml), with a comment giving the date and the dataset size, as the manifest's thresholds must:

```yaml
  # Calibrated <date> on <n> labelled diffs (<k> risky): recall <r>, safe skip rate <s> (scripts/calibrate.py).
  - name: low_risk
    question: needs_review
    rule: p_below
    value: <threshold>
    action: skip
```

Replace the placeholder comment above `gates:` at the same time.

A gate alters behaviour, so it needs a minor version bump, 0.1.0 to 0.2.0 (docs/CONVENTIONS.md, "Agent address"). `scripts/check_version_bump.py` compares only the prompt, the schemas and the `questions` block, so it does not catch a gate change made without a bump. The agent has one version directory, so after the bump nothing matches `diff-triage@^0.1`, and the bump must also change the range to `^0.2` in `TRIAGE` in [pipelines/review_panel.py](../pipelines/review_panel.py) and in [tests/live/test_jev_live.py](../tests/live/test_jev_live.py). The 2026-10-27 gate was committed at 0.1.0 and the bump follows in its own commit, so the two can merge back to back; `ROUTE_ONLY` keeps the gate from skipping anything in between. [tests/live/test_triage_live.py](../tests/live/test_triage_live.py) asserts that the sample diff's `skip` matches the manifest's skip gate: below the threshold, not escalated and not truncated.

The panel resolves with `published_only=True`. Until `factory publish-check diff-triage@^0.3` publishes 0.3.0, a panel run stops with exit 3 (`NotPublished`) unless it is given `--allow-unpublished`, so publish right after the merge.

## Shadow mode

Sources: [scripts/shadow_report.py](../scripts/shadow_report.py), `ROUTE_ONLY` in [runtime/gates.py](../runtime/gates.py).

`ROUTE_ONLY` stays `True` for the whole shadow period. The review panel triages every diff, writes the `Decision` and the probabilities to a `triage:decision` trace row, and still runs every reviewer. The 14-day clock starts on the day the calibrated gate is merged and published.

`scripts/shadow_report.py` reads those rows for a range of UTC days (default: the 14 days ending today) and decides each diff again at the skip threshold, the manifest's skip gate by default or `--threshold`. For every diff that would have been skipped it reports the most severe finding the panel's reviewers and judge made under the same pipeline run id, and what the run cost. Runs of `calibrate.py`, whose ids start with `calibrate-`, are left out. A row whose answers do not fit the manifest's questions, such as one written by another version of the agent, is counted as unreadable. The store is opened read-only.

What a would-skip diff's finding means:

| Worst finding | Meaning | Action |
| --- | --- | --- |
| blocker | The threshold is too high | Exit 1. Raise the threshold, or recalibrate, and restart the 14-day clock |
| major | A miss the recall target allows for, but worth reading | Reported; exit 1 only with `--fail-on major` |
| minor, info or none | What a skip is for | Reported |

Exit codes: 0 no would-skip diff has a finding at `--fail-on` severity or worse, 1 at least one does or the store cannot be opened, 2 bad arguments, 3 no trace store or no traces table, 4 a bad manifest.

DuckDB allows one read-write process per file. A report that runs while a panel is writing to the same store fails with exit 1 and a hint; run it again later.

### Running it once

Nothing is scheduled, and nothing needs to be. The report is a query over the trace store, so one run at the end of the window covers the whole period: `--since` the day the clock started, `--until` the last day, and `--fail-on major` to get the exit code the decision needs. A daily run would only split the same rows across days, and a scheduled job on a laptop that is asleep at the appointed hour silently misses them anyway.

```bash
set -a; . ~/.config/agb/secrets.env; set +a
FACTORY_TRACE_DB=~/.local/share/factory/traces.duckdb \
  uv run python scripts/shadow_report.py --since 2026-10-30 --fail-on major
```

Add `--json` and redirect to `data/shadow.jsonl` to keep the document. Exit 0 means no would-skip diff had a `major` or `blocker` finding; exit 1 names the diff.

What the window measures is panel runs, so the evidence only exists if `factory review` runs against the shared store during the period. Reviews that write to a different `FACTORY_TRACE_DB`, or none, are invisible to the report.

## Shadow results

Started 2026-10-30. `factory publish-check diff-triage@^0.3` passed that day at commit b5040e5 (7 of 7, recall on risky cases 1.0), which publishes diff-triage@0.3.0 with the `low_risk` skip gate at 0.5 and `unsure` yielding to it. `ROUTE_ONLY` stays on. The 14 days end at the earliest on 2026-11-13. The first report, run by hand on 2026-10-30 against the shared store (`FACTORY_TRACE_DB=~/.local/share/factory/traces.duckdb`), covered 2026-10-17 to 2026-10-30 and found no panel runs, so there is nothing to record yet; the sweep's own runs are left out by their `calibrate-` run ids. The final report, run as in [Running it once](#running-it-once) on or after 2026-11-13, goes in the table below.

| Window | Panel runs | Would skip | Worst finding | Note |
| --- | --- | --- | --- | --- |
| 2026-10-17 to 2026-10-30 | 0 | 0 | none | first report, by hand, before any review ran |

Record the end-of-window report above, and any threshold change with the day the clock restarted.

## Measured against --no-triage

`scripts/triage_saving.py` runs the review panel over every fixture twice, once with diff-triage and once as `factory review --no-triage` would, and reads both costs back from the trace (the script's docstring has the method). It ran once on 2026-10-27 at commit e1ac33d, with diff-triage@0.2.0 (skip gate `needs_review` below 0.5), code-reviewer@1.2.0, security-reviewer@1.0.0, perf-reviewer@0.3.0 and judge@2.0.0, a $5.00 budget per run and the judge gate off:

```bash
uv run python scripts/triage_saving.py --allow-unpublished --trace-db data/triage-saving.duckdb --json
```

It exited 0: every run went through and no would-skip fixture had a `major` finding or worse.

| Fixture | With triage | No triage | Jev | Decision | Route | Worst finding, either run |
|---|---:|---:|---:|---|---|---|
| clean-refactor | $0.1241 | $0.0578 | $0.000047 | skip (low_risk) | code-reviewer | - |
| docs-only | $0.0423 | $0.0426 | $0.000025 | escalate (unsure) | code-reviewer, security-reviewer, perf-reviewer | - |
| hardcoded-secret | $0.0562 | $0.0615 | $0.000034 | review (reviewers=code-security) | code-reviewer, security-reviewer | blocker |
| missing-index-hint | $0.0552 | $0.0619 | $0.000037 | review (reviewers=code-perf) | code-reviewer, perf-reviewer | - |
| mixed | $0.0628 | $0.0522 | $0.000036 | review (reviewers=code) | code-reviewer | - |
| n-plus-one | $0.0988 | $0.0931 | $0.000037 | escalate (unsure) | code-reviewer, security-reviewer, perf-reviewer | major |
| off-by-one | $0.0509 | $0.0516 | $0.000023 | review (reviewers=code-perf) | code-reviewer, perf-reviewer | major |
| off-by-one-red | $0.0504 | $0.0506 | $0.000023 | review (reviewers=code-perf) | code-reviewer, perf-reviewer | major |
| off-by-one-single-file | $0.0486 | $0.0495 | $0.000020 | review (reviewers=code) | code-reviewer | major |
| path-traversal | $0.0493 | $0.0491 | $0.000028 | review (reviewers=code-security) | code-reviewer, security-reviewer | - |
| quadratic-loop | $0.0733 | $0.0751 | $0.000035 | review (reviewers=code-perf) | code-reviewer, perf-reviewer | blocker |
| sql-injection | $0.0865 | $0.0834 | $0.000033 | review (reviewers=code-security) | code-reviewer, security-reviewer | blocker |
| test-only | $0.0493 | $0.0480 | $0.000021 | skip (low_risk) | code-reviewer | - |
| untested-change | $0.0409 | $0.0451 | $0.000021 | skip (low_risk) | code-reviewer | - |
| untested-change-covered | $0.0423 | $0.0423 | $0.000031 | skip (low_risk) | code-reviewer | - |

| Total | |
|---|---:|
| With triage, 15 runs | $0.9308 |
| No triage, 15 runs | $0.8637 |
| Difference | $0.0671 |
| Jev overhead, 15 triage calls | $0.000453 |
| Would skip | 4 of 15 |
| Projected saving | $0.2561 |
| Would-skip fixtures with a `major` finding or worse | 0 |
| Measurement spend | $1.7945 |

`ROUTE_ONLY` still holds, so the with-triage runs ran every reviewer: each of the four reviewers and the judge has 30 rows in the store, one per run, and the $0.0671 difference is run-to-run variance, not triage. The saving is therefore projected: the four skipped fixtures' reviewer and judge cost in their with-triage runs, $0.2565, less Jev's cost over all 15 fixtures. That is 28% of the with-triage spend, against a Jev overhead of 0.05%. Most of it is clean-refactor, whose with-triage run cost $0.1241 against $0.0578 for the identical panel without triage; priced from the no-triage runs instead, the four skips come to $0.1932 and the saving to $0.1927, 22% of the no-triage spend.

Every fixture with a `major` or `blocker` finding was reviewed: the three blockers (hardcoded-secret, quadratic-loop, sql-injection) and the four off-by-one and n-plus-one majors all went to the panel, and docs-only escalated as `unsure`. Nine of the eleven reviewed fixtures had a narrower route; they take effect only once `ROUTE_ONLY` is off, so this run does not measure what routing saves. `FACTORY_TRACE_DB=data/triage-saving.duckdb factory cost --since 2026-10-27 --by kind` over the same store shows the split: `claude` 120 runs, $1.7941; `jev` 30 rows, $0.0005, which are the 15 triage calls and their 15 zero-cost `triage:decision` rows.

## After the shadow period

Stage 4 of #20, not built yet: once 14 days pass with no would-skip blocker, a manifest flag turns `ROUTE_ONLY` off for diff-triage, and the panel acts on `skip` and `route`. A permanent 5% random sample of skipped diffs still gets the full panel, so a drift in what Jev skips shows up as findings on sampled diffs rather than as nothing at all.

## Re-running calibration

The threshold belongs to one model id. When `model` in the manifest changes:

1. Change `model` in [agents/diff-triage/agent.yaml](../agents/diff-triage/agent.yaml) and its price in [runtime/pricing.py](../runtime/pricing.py).
2. Run the live model-id check: `uv run pytest -m live_jev tests/live/test_jev_live.py`.
3. Run `scripts/calibrate.py` as in [Method](#method), with `--trace` on a scratch file.
4. Paste the new gate, bump the version as in [Writing the manifest snippet](#writing-the-manifest-snippet), and record the run under [Results](#results).
5. Restart the 14-day shadow clock.

Re-run it as well when a shadow report shows a would-skip blocker.
