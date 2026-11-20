# Work items and `factory ticket`

A work item, or ticket, is one unit of work the factory can take from idea to pull request. This
page is how to use them. The rules (the fields, the eight states, the legal moves and the order
of the `ready` items) are in [CONVENTIONS.md](CONVENTIONS.md#work-items), and the reasons are in
[ADR 0002](decisions/0002-work-items.md).

## Where items live

Items are two tables in the trace store, `work_items` and `work_item_events`, added by migrations
4 and 5 of `runtime/trace.py`; migration 6 adds `escalations`, the questions stopped runs ask. A store written before them is upgraded the first time any command
opens it, and its trace rows are kept. `factory ticket` uses the same file as every other command:
`FACTORY_TRACE_DB`, or `data/traces.duckdb` when it is unset. DuckDB lets one process write a file
at a time, so do not run `factory ticket` against a store a pipeline is writing.

Code uses `runtime.work_items.WorkItems`, built on an open `Trace`:

```python
from runtime.trace import Trace
from runtime.work_items import WorkItems

with Trace() as trace:
    store = WorkItems(trace)
    item = store.add(ticket, depends_on=["wi-3"])
    store.move(
        item.id,
        "blocked",
        "the sandbox image is missing",
        actor="pipeline:build_loop",
        pipeline_run_id=run_id,
    )
```

## The commands

Every subcommand takes `--json`, which prints one JSON object on stdout. `add`, `show`, `move` and
`link` all print the item as `show` does.

| Command | What it does |
| --- | --- |
| `factory ticket add --kind K --title T --acceptance A [--acceptance A ...] --repo R --ref REF` | Writes a new item in `todo`. Optional: `--body`, `--parent`, `--depends-on` (repeatable), `--impacted-path` (repeatable), `--size S\|M\|L` and `--reason` (default `created`) |
| `factory ticket list [--status S]` | Every item, or those in one state, in id order |
| `factory ticket show <id>` | One item with its cost so far and every event |
| `factory ticket move <id> <state> --reason R` | Makes one legal move, as actor `human` |
| `factory ticket link <id> <pipeline_run_id>` | Adds a pipeline run to the item, so its cost counts. Linking a run twice changes nothing |
| `factory ticket ready` | Promotes and blocks `todo` items, then lists the `ready` ones in the order a pipeline should take them |

```console
$ factory ticket add --kind docs --title "Write the ADR" --acceptance "the ADR is merged" \
    --repo agb1986/agb-agent-factory --ref main
wi-1  todo  docs  Write the ADR
...
$ factory ticket add --kind feature --title "Build the store" --acceptance "tests pass" \
    --repo agb1986/agb-agent-factory --ref main --depends-on wi-1 --size M
$ factory ticket ready
promoted wi-1
1 ready:
  wi-1	docs	Write the ADR
$ factory ticket move wi-2 ready --reason "skip the queue"
wi-2 cannot be ready: wi-1 not done
```

`--parent` takes another item's id or `prd:<pipeline_run_id>`. Each `--depends-on` must name an
existing item. A title is one line of at most 120 characters, `--repo` is `owner/name` or an
absolute path, and each `--impacted-path` is relative to the repository root.

## What `ready` does

`factory ticket ready` calls `WorkItems.ready()`, which walks the `todo` items in id order:

- An item with a `dropped` dependency moves to `blocked`, with the reason
  `dependency wi-<n> was dropped`. A person decides what happens to it next.
- An item whose dependencies are all `done`, or that has none, moves to `ready`.
- Any other item stays in `todo`.

These moves are logged with actor `store`, and `store` may make no other move. The `ready` items
come back ordered: incidents first, then the items more others are waiting on (dependents that
are `dropped` do not count), then the older, then the lower id. The Jev ranking key of
CONVENTIONS is left out until a ranking agent exists. Running `ready` again with nothing changed
promotes nothing.

An `incident` has no dependencies, so `ready` promotes every one left in `todo`, including the
ones the incident pipeline filed there because no test reproduced the event (#108). For an
incident, `ready` therefore does not mean reproduced: the hotfix pipeline (#109) checks
`pipelines.incident.is_reproduced` on the body before it acts, as [incident.md](incident.md)
describes.

## Exit codes

| Code | When |
| --- | --- |
| 0 | The command did what it said |
| 2 | A bad argument or field, an empty reason, an illegal move, a move to the state the item is in, or a dependency cycle. The hint of an illegal move lists the legal ones, such as `from todo: blocked, dropped, ready` |
| 3 | An unknown item id, as the item, a parent or a dependency |

Under `--json` an error leaves stdout empty and prints the error envelope on stderr, as every
command does ([CONVENTIONS.md](CONVENTIONS.md#errors)).

## Changing dependencies

`WorkItems.set_depends_on(id, depends_on, reason)` replaces an item's dependencies while it is
`todo` or `blocked`, and refuses it in any other state. It is the one write that could close a
cycle, because a new item cannot be a dependency of anything yet, so the acyclic check runs there
and refuses a cycle with its path, such as `dependency cycle: wi-1 -> wi-3 -> wi-2 -> wi-1`.
There is no command for it yet.

## Writing a batch

`WorkItems.add_many(batch, parent=..., actor=..., pipeline_run_id=..., reason=...)` writes several
items in one transaction, for a caller that has a whole plan (#92). Each `NewItem` holds a ticket, a
size and `depends_on` as indexes into the batch: any position but its own, earlier or later, as
long as the graph stays acyclic. Those become the new `wi-<n>` ids. The whole batch is checked before anything is written, the
acyclic check runs over the full graph, and any error rolls back, so either every item is stored,
with its creation event, or none is. `add` is the one-item case.

## Building an item

`factory build <id>` takes one `ready` item to a reviewed pull request and moves it to
`in_progress`, then to `in_review`, `escalated` or `blocked`, with actor `pipeline:build_loop`. A dry run links
its run to the item but moves nothing. [build-loop.md](build-loop.md) describes the loop (#87).

## Escalations and `factory escalations`

When a run stops on something a person can settle, such as a `blocked` coder, a budget stop or a
second request for changes, it writes an escalation record and moves the item to `escalated`
(#93). The record holds the question, the runs, the cost at the stop, the diff, the last agent
outputs and the end of the suite output. The rules, and the table of which stop goes where, are in
[CONVENTIONS.md](CONVENTIONS.md#escalations).

| Command | What it does |
| --- | --- |
| `factory escalations list [--open]` | Every record, oldest first, or only the unanswered ones |
| `factory escalations show <id>` | One record as Markdown, its summary first when it has one, then the diff, the outputs and the suite tail in `<details>` sections; `--json` adds `summary` (null when there is none) |
| `factory escalations answer <id> --text TEXT\|@FILE` | Store the answer and, when no other record on the item is open, move the item to `ready` |

When a run writes a record, the escalation-summary agent (#94) also writes a summary of it: one
paragraph on what is blocked and what the person must decide, and up to four options, each with
what it costs and a quote from the record that backs it. A summary whose quotes are not in the
record is not stored, and a budget stop gets none; `show` then prints the record alone. The rules
are in [CONVENTIONS.md](CONVENTIONS.md#summaries).

Each takes `--json`. A typical round:

```sh
factory escalations list --open
factory escalations show esc-1
factory escalations answer esc-1 --text "Put the parser in widgets/parse.py."
factory build wi-4
```

The next `factory build` gives the coder the ticket with the answer appended to its body under
`## Answers to earlier escalations`; the stored ticket is not changed. An answer is stored once
and never replaced. An answer to an item a person has already moved on by hand is refused, and
the record stays open. Exit codes are as above: 2 for a bad or refused answer or an unreadable
file, 3 for an unknown id.

Code uses `runtime.escalations`:

```python
from runtime.escalations import answer, escalate, render_markdown

with Trace() as trace:
    record = escalate(
        trace,
        item_id,
        trigger="blocked_output",
        question="Which module owns the parser?",
        reason="CoderBlockedError: the coder is blocked",
        actor="pipeline:build_loop",
        pipeline_run_id=run_id,
    )
    print(render_markdown(record))
    answer(trace, record.id, "widgets/parse.py", "cli")
```
