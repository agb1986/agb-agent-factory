# Golden JSON documents

These files pin the `--json` output of the `factory` commands. `tests/test_golden_cli.py` runs each
command through `factory.cli.main` with a fake model, parses the one document on stdout, replaces
the fields that change on every run (`duration_ms`, `git_sha`, `pipeline_run_id`, `eval_run_id`, `run_id`) with
fixed values, and compares the result to `tests/golden/<name>.json`.

| File | Command |
| --- | --- |
| `list.json` | `factory list --json` |
| `eval.json` | `factory eval code-reviewer@^1.2 --json` |
| `publish-check.json` | `factory publish-check code-reviewer@^1.2 --json` |
| `review.json` | `factory review <repo> --json` |
| `triage.json` | `factory triage <repo> --json` |
| `fix.json` | `factory fix <repo> --finding <json> --json` |
| `incident.json` | `factory incident <repo> --event <json> --json` |
| `build.json` | `factory build wi-1 --repo <repo> --json`, on a ready item |
| `plan.json` | `factory plan <repo> --prd @file --approve --json`, after a first `--idea` run |
| `cost.json` | `factory cost --since 2026-10-20 --json` |
| `cost-by-kind.json` | `factory cost --since 2026-10-20 --by kind --json` |
| `ticket-show.json` | `factory ticket show wi-2 --json`, on a seeded store |
| `escalation-show.json` | `factory escalations show esc-1 --json`, on a seeded store |
| `deps.json` | `factory deps <repo> --impact pkg/b.py --json` |
| `run.json` | `factory run answerer@^0.1 --input payload.json --json`, on a test agent |

## When a golden test fails

The output of a command changed. If that is what you meant, rewrite the files and read the diff:

```console
$ uv run pytest tests/test_golden_cli.py --update-golden
$ git diff tests/golden
```

If it is not what you meant, fix the code. The `eval`, `publish-check` and `review` files name the
real `code-reviewer`, `security-reviewer`, `perf-reviewer` and `judge` agents, so a version bump of
one of them also needs a refresh.

## Add a command in one step

Add one test to `tests/test_golden_cli.py` and run it with `--update-golden`:

```python
def test_triage_json(capsys, golden):
    golden("triage", _run(["triage", "--json"], capsys))
```

`_run` checks the exit code (pass `code=ExitCode.ERROR` for a negative result), returns the
normalised document, and `golden("triage", ...)` writes or checks `tests/golden/triage.json`. If
the command calls the model, put a fake in front of it the way `fake_eval`, `FakeReviewRunner` and `FakeJev` do.
If it adds a new per-run field, add its name and stand-in value to `VOLATILE`.

`fix.json` runs `factory fix` on the `off-by-one-red` fixture with a fake test-writer, fixer and
sandbox (`FixRunner` and `FakeSandbox` in `tests/pipelines/test_fix_loop.py`). Its test also
replaces the repository path, the two output paths and the fix commit, which change per run.

`incident.json` runs `factory incident` on the `crash-empty-average` fixture with the event of
the `empty-ratings-divide-by-zero` eval case, a fake triage-diagnostics and test-writer and a fake
sandbox (`IncidentRunner` and `FakeSandbox` in `tests/pipelines/test_incident.py`). Its test also
replaces the repository path, which appears twice, and the report path.

`build.json` runs `factory build` on a one-commit repository whose `origin` is the fake
`acme/widgets`, with the fake coder of `tests/pipelines/test_build_loop.py` (`BuildRunner`), the
fake panel, `FakeSandbox` and the fake `gh`. Its test also replaces the pipeline run id inside the
branch and the body, the commits and the paths under `tmp_path`.

`cost.json` is built from a seeded trace store, not from a fake model; `tests/runtime/golden/`
holds the same report as text and grouped by pipeline.

The review panel also has a golden of its own, `tests/pipelines/golden/review-panel.json`, which
pins the pipeline document with a failing reviewer. `review.json` here pins what the command prints.
