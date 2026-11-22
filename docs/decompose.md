# The decompose pipeline

`factory decompose` takes a repository and one named feature to a `refactor` ticket in the work-item store (#112). It composes two agents of team `decomp` and the store: `code-archaeology` reads the codebase from an entry point, `extraction-modularization` takes what it found and draws the boundary of the feature, and the pipeline files the `ExtractionBlueprint` that comes back as one ticket. The code is `pipelines/decompose.py`. The store is described in [work-items.md](work-items.md) and the rules it follows are in [ADR 0002](decisions/0002-work-items.md).

**The command has not been run against a model.** Everything below is tested with a fake runner. The first live run is the operator's, and it is what epic #119 is waiting for ([The first live run](#the-first-live-run)).

The agents write nothing. Both have read-only tools (`git.read_file`, `rg.search`, `deps.graph`, `deps.impact`, `deps.call_graph`, and `deps.dead_code` for code-archaeology) and no sandbox. The pipeline is the only writer of the store, with actor `pipeline:decompose`. It moves no code, commits and pushes nothing, and writes nothing to GitHub ([ADR 0003](decisions/0003-outside-writes.md)).

## The steps

```console
$ factory decompose ./repo --entry-point legacy_billing/cli.py::main \
    --feature "Late fees on overdue invoices" --start legacy_billing/billing.py::apply_late_fees
decompose run <run-id>: Late fees on overdue invoices
  commit f2cd880ea1be, read from legacy_billing/cli.py::main
  starts at legacy_billing/billing.py::apply_late_fees
  symbols that move: <count>
  open questions: <count>
filed wi-1 (todo): Extract Late fees on overdue invoices
cost $<cost>
```

1. Both agents are resolved, and the request is checked: the symbols and the name by the agents' own input models, the commit, that the file of each symbol is in that commit, and the name the ticket gives the repository. This happens before any model call, so a bad request costs nothing. Whether the symbols themselves exist is left to the agents.
2. The repository is cloned into a throwaway directory (`pipelines.sandbox.checkout`) at that commit. Nothing reads the source work tree after this.
3. code-archaeology reads the clone from `--entry-point` and returns the module map, the data flows, the dead code, the symbols a dynamic lookup may reach and the execution paths.
4. extraction-modularization gets that output with the feature's name and start symbol, reads the same clone, and returns the blueprint. Its run is a child of the archaeology run (`parent_run_id`).
5. The pipeline files one ticket and links the run to it. The clone is removed whatever happened.

Both agent runs carry one pipeline run id, so `Trace.cost_by_pipeline_run` gives the whole cost, and the run is linked to the ticket with `link_run`. A run that fails (an agent error, a budget overrun) files nothing.

## The commit

The ticket's `ref` is always a full commit id, and the agents read exactly that commit, so a blueprint never describes code its ticket does not name.

- Without `--ref` it is the commit of `HEAD`, and a work tree with uncommitted changes is refused with exit 2. Staged, unstaged and untracked files count; ignored files do not.
- With `--ref`, `HEAD` included, it is the commit the ref names. Uncommitted changes do not stop the run and are never read.

## The ticket

- `kind` is `refactor`, and `title` is `Extract <feature>`, cut at 120 characters; the body has the name whole.
- `body` is the blueprint twice. The markdown comes first: the feature and its start symbol, the summary, then the sections Inputs, Outputs, Invariants, Symbols that move, Outside symbols the feature uses, Outside code that calls the feature, Shared state (with how each tie can be cut), Tests that pin the behaviour, and Open questions. The section `Blueprint as JSON` follows, which `blueprint_of` reads back to an equal blueprint. Text added below it is ignored.
- `acceptance` is the invariants, then one `<path> passes unchanged: <what it pins>` for each pinning test file.
- `repo` is `--repository`, or the absolute path of the repository. `ref` is the commit.
- `impacted_paths` are the modules the moving symbols are in.

The ticket is filed in `todo`, always. Read the blueprint and its open questions before `factory ticket ready`: that command promotes the item, which has no dependencies, to `ready`, and from there `factory build` gives it to `refactorer` and its [behaviour check](build-loop.md#the-behaviour-check-refactor). Nothing between the two agents and the build loop judges whether the blueprint is right.

## Exit codes and the JSON document

| Exit | When |
| --- | --- |
| 0 | The ticket was filed |
| 1 | The run failed or went over budget; nothing was filed |
| 2 | Usage: a symbol, the feature name, `--ref` or `--repository` is malformed; the work tree has uncommitted changes and no `--ref` was given; `<repo>` is not the top of a git work tree; an agent is not the kind the pipeline needs |
| 3 | The repository, the `--ref` commit, the file of a symbol or an agent does not exist |
| 4 | `ANTHROPIC_API_KEY` is not set (checked after the request) |

Under `--json`, stdout carries one document and the text goes to stderr. The document has `pipeline_run_id`, `repo_path`, `commit`, `entry_point`, `feature` (`name`, `start`), `archaeology` and `extraction` (each `ref` and `output`), `ticket` (`id`, `status`, `kind`, `title`, `body`, `acceptance`, `repo`, `ref`, `impacted_paths`), `total_cost_usd` and `duration_ms`. `tests/golden/decompose.json` pins it. No report file is written: the ticket holds the blueprint, and the archaeology's output is in the JSON document only.

## Options

`factory decompose <repo> --entry-point SYMBOL_OR_PATH --feature NAME --start SYMBOL [--ref REF] [--repository NAME] [--json] [--allow-unpublished] [--budget-usd N]`. A symbol is `path.py::name`; `--entry-point` also takes the path of a module. `--repository` is `owner/name` or an absolute path. Both agents (`code-archaeology@^0.1`, `extraction-modularization@^0.1`) are resolved before the first request, published versions only unless `--allow-unpublished`, and the pipeline refuses one with a sandbox or a files policy. The run is charged to one `Budget`.

## The first live run

The target is the fixture `legacy-billing` ([ADR 0005](decisions/0005-decomposition-target.md)). With `ANTHROPIC_API_KEY` set:

```sh
uv run python -m fixtures.build legacy-billing
uv run factory decompose fixtures/repos/legacy-billing \
  --entry-point legacy_billing/cli.py::main \
  --feature "Late fees on overdue invoices" \
  --start legacy_billing/billing.py::apply_late_fees \
  --allow-unpublished
```

Leave out `--allow-unpublished` once both agents have passed `factory publish-check`. The run is paid and files a ticket in the shared store.

## Testing

`tests/pipelines/test_decompose.py` runs the pipeline and the command with a fake runner, a git repository and a DuckDB file in a temporary directory, with no key and no network. Not verified, because each needs a model: a live run of the command, which has no live test; `factory eval` and `factory publish-check` of either agent; and whether a real blueprint is good enough for `refactorer` to build from.
