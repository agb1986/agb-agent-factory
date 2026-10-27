# Diff-triage calibration

How the diff-triage skip threshold is calibrated (#20): the labelled diffs, the sweep that picks the threshold, how the result goes into the manifest, and the shadow period before anything is skipped. Every fact below was checked on 2026-10-27 against the code and files linked in its section.

The live steps have not run. The Anthropic and TypeSafe keys were revoked on 2026-10-27 while the labelled set was being built, so the panel labelling pass, the live model-id check and the real sweep are still owed. [Results](#results) and [Shadow results](#shadow-results) are placeholders until they run. Everything that needs no key is in place: the labels, [scripts/calibrate.py](../scripts/calibrate.py), [scripts/shadow_report.py](../scripts/shadow_report.py) and their tests against fakes.

## Dataset

Checked 2026-10-27. Sources: [evals/calibration/labels.yaml](../evals/calibration/labels.yaml), its schema in [evals/calibration/labels.py](../evals/calibration/labels.py).

The set has 126 merged pull requests, all in repositories owned by github.com/agb1986. A row stores the repository URL and a commit pair, never source: `base` is the first parent of the merge commit and `head` is the merge commit, so the diff is exactly what the pull request merged. [evals/calibration/fetch.py](../evals/calibration/fetch.py) fetches it again at run time through `gh api .../compare/{base}...{head}`, so `gh` must hold a token that can read the 94 private rows.

A diff is risky when its worst finding would be major or blocker on the reviewers' scale ([agents/code-reviewer/system.md](../agents/code-reviewer/system.md)), and safe otherwise. Each row has a one-line reason.

| Count | Value |
| --- | --- |
| Diffs | 126: 91 safe, 35 risky |
| Worst finding | none 45, minor 46, major 33, blocker 2 |
| `needs_panel` | 28: 21 risky, 7 safe |
| Private | 94 |
| Label source | every row `reader`; no `panel` labels yet |
| Labelling spend | $0.00 of the $15 budget |

| Repository | Safe | Risky | Private |
| --- | --- | --- | --- |
| agb-agent-factory | 22 | 1 | 23 |
| agb-tools | 10 | 3 | 13 |
| agb-trader | 11 | 10 | 21 |
| ai-data-store | 3 | 4 | 0 |
| cannabot | 12 | 6 | 18 |
| finance-agent | 3 | 0 | 0 |
| torrent-agent | 14 | 8 | 0 |
| xelpfin | 16 | 3 | 19 |

Every label is a reader label: someone read the diff and judged whether a careful reviewer would raise a major or blocker finding. Docs-only, test-only and dependency-bump diffs were labelled from their file lists and contents; the rest were read in full. A row the reader was unsure of carries `needs_panel: true`. Where the doubt was whether a major finding exists at all, the row is labelled risky at major, so doubt can only make the measured recall stricter. The 28 `needs_panel` rows should be checked against a full panel run before the real sweep, and their `source` set to `panel`.

Fetched diffs are untrusted data. They are handed to the triage agent as text and never run, and neither script prints a diff, a file name or a finding's text.

### Truncation

Checked 2026-10-27 with `uv run python scripts/calibrate.py --dry-run`, which fetches the diffs and measures them without a Jev call.

The manifest's `input.max_chars` is 24000. Rendered sizes have a median of 5948 characters, a 90th percentile of 19431 and a maximum of 649532. Five of the 126 diffs exceed `max_chars`, four safe and one risky. A truncated input is always reviewed (`runtime.gates.decide`), so no threshold can skip more than 87 of the 91 safe diffs: 95.6%.

## Method

Sources: [scripts/calibrate.py](../scripts/calibrate.py), [evals/calibration/sweep.py](../evals/calibration/sweep.py), [runtime/gates.py](../runtime/gates.py).

`scripts/calibrate.py` fetches every labelled diff and runs diff-triage over each one once, through the review panel's `run_triage`. The calls share one `Budget` (`--budget-usd`, default $1.00). Each call is traced under the pipeline run id `calibrate-<run>-<label id>`, and its cost is read back by that id.

The sweep then decides every diff again at each candidate threshold with `runtime.gates.decide`, on a copy of the manifest whose `low_risk` skip gate (`needs_review` `p_below` the threshold) is set by `runtime.gates.with_skip_threshold`. The numbers are what the panel would do: the `unsure` gate still escalates and clears the skip, and a truncated input is always reviewed. The default thresholds are 0.02, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4 and 0.5; `--thresholds` replaces them.

At each threshold the sweep reports:

| Column | Meaning |
| --- | --- |
| Recall on risky diffs | Risky diffs still reviewed, over all risky diffs |
| Skip rate on safe diffs | Safe diffs skipped, over all safe diffs |
| Cost per diff | Mean Jev cost, plus the share of diffs still reviewed times `--panel-cost-usd` when given |
| Missed | The risky diffs this threshold would skip |

The chosen threshold is the one that skips the most safe diffs among those whose recall reaches the manifest's `evals.min_recall_risky` (0.98). A tie goes to the lower threshold, which leaves more margin. A diff whose Jev call failed is left out of every number, listed, and makes the script exit 1, so an incomplete sweep is never mistaken for a result. A rejected key stops the run at the first call with exit 4.

Run it from the repository root once the keys work and the `needs_panel` rows are settled:

```sh
set -a; source ~/.config/agb/secrets.env; set +a
uv run python scripts/calibrate.py --panel-cost-usd 0.35 --json --trace /tmp/calibrate.duckdb
```

`--panel-cost-usd` is the cost of one full panel review; take it from recent panel runs with `factory cost --since <date> --by pipeline`. Without it, the cost column is the Jev cost alone.

Point `--trace` at a scratch file and delete it afterwards. The Jev runner records its full payload, the diff, as `input_json` in the trace store, so a run against the default store copies every labelled diff, private ones included, into `data/traces.duckdb`. Some labelled diffs contain credentials committed in their own repositories: the sweep sends them to TypeSafe and the trace store keeps them, so rotate those credentials before the real sweep.

At jev-1.14.0 prices, $0.042 per million input tokens with output free ([runtime/pricing.py](../runtime/pricing.py)), and with input cut at 24000 characters, one call is at most about 6000 tokens, or about $0.00025. The whole set should cost well under $0.05. This is an estimate, not a measurement.

Exit codes: 0 a threshold reaches the recall, 1 none does or a call failed, 2 bad arguments, 3 no labels file, 4 a missing or rejected key, or `gh` cannot read a repository.

## Results

Not run: keys revoked 2026-10-27. When the sweep runs, record here the date, the model id, the number of diffs measured and truncated, the table the script prints, the chosen threshold and the risky diffs any lower-recall threshold missed.

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

A gate alters behaviour, so the same commit bumps the minor version, 0.1.0 to 0.2.0 (docs/CONVENTIONS.md, "Agent address"). `scripts/check_version_bump.py` compares only the prompt, the schemas and the `questions` block, so it does not catch a gate change made without a bump. The agent has one version directory, so after the bump nothing matches `diff-triage@^0.1`, and the same commit must also change the range to `^0.2` in `TRIAGE` in [pipelines/review_panel.py](../pipelines/review_panel.py) and in [tests/live/test_jev_live.py](../tests/live/test_jev_live.py). [tests/live/test_triage_live.py](../tests/live/test_triage_live.py) asserts that its sample diff does not skip because there is no skip gate yet; check that assertion against the new gate.

The panel resolves with `published_only=True`. Until `factory publish-check diff-triage@^0.2` publishes 0.2.0, a panel run stops with exit 3 (`NotPublished`) unless it is given `--allow-unpublished`, so publish right after the merge.

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

### Running it daily

Nothing is scheduled. `agb` has no scheduling verb as of 2026-10-27, so a daily report is a cron entry or a systemd user timer. Neither reads your shell profile, so replace `uv` with the full path that `command -v uv` prints.

```cron
0 7 * * * cd /path/to/agb-agent-factory && uv run python scripts/shadow_report.py --json >> data/shadow.jsonl
```

The systemd alternative is two files, `~/.config/systemd/user/shadow-report.service` and `shadow-report.timer`:

```ini
# shadow-report.service
[Service]
Type=oneshot
WorkingDirectory=/path/to/agb-agent-factory
ExecStart=/bin/sh -c 'uv run python scripts/shadow_report.py --json >> data/shadow.jsonl'

# shadow-report.timer
[Timer]
OnCalendar=*-*-* 07:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it with `systemctl --user enable --now shadow-report.timer`. Either one appends one JSON document a day to `data/shadow.jsonl`; a non-zero exit shows in the cron mail or in `journalctl --user -u shadow-report`.

## Shadow results

Not started: the shadow period begins once a calibrated gate is merged and published. Record here the start date, the threshold, each daily report's would-skip count and worst finding, and any threshold change with the day the clock restarted.

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
