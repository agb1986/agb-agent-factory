# Golden JSON documents

These files pin the `--json` output of the `factory` commands. `tests/test_golden_cli.py` runs each
command through `factory.cli.main` with a fake model, parses the one document on stdout, replaces
the fields that change on every run (`duration_ms`, `git_sha`, `pipeline_run_id`, `eval_run_id`) with
fixed values, and compares the result to `tests/golden/<name>.json`.

| File | Command |
| --- | --- |
| `list.json` | `factory list --json` |
| `eval.json` | `factory eval code-reviewer@^1.2 --json` |
| `publish-check.json` | `factory publish-check code-reviewer@^1.2 --json` |
| `review.json` | `factory review <repo> --json` |
| `triage.json` | `factory triage <repo> --json` |

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

`factory cost` and `factory fix` have no golden file yet because the commands do not exist.

The review panel also has a golden of its own, `tests/pipelines/golden/review-panel.json`, which
pins the pipeline document with a failing reviewer. `review.json` here pins what the command prints.
