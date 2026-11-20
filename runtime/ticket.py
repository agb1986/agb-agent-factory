"""`factory ticket`: add, list, show, move and link work items, and promote the ready ones.

Each function here is one subcommand. It opens the trace store named by `FACTORY_TRACE_DB` (see
`runtime/trace.py`), works through `runtime.work_items.WorkItems` with actor `human`, prints text
or one JSON document on stdout, and returns 0. Failures are raised, not printed: the CLI's root
handler maps `InvalidWorkItem` and `IllegalTransitionError` to exit 2 and `WorkItemNotFound` to 3.
"""

import json
from collections.abc import Sequence
from typing import Any, cast

from pydantic import ValidationError

from registry.common_schemas import Ticket
from runtime.trace import Trace
from runtime.work_items import InvalidWorkItem, Size, WorkItem, WorkItems


def add(
    *,
    kind: str,
    title: str,
    body: str,
    acceptance: Sequence[str],
    repo: str,
    ref: str,
    parent: str | None = None,
    depends_on: Sequence[str] = (),
    impacted_paths: Sequence[str] = (),
    size: str | None = None,
    reason: str = "created",
    as_json: bool = False,
) -> int:
    """Write a new item in `todo` and print it as `show` does. Returns 0.

    Raises `InvalidWorkItem` for a field the `Ticket` model refuses or a size other than S, M or
    L, and whatever else `WorkItems.add` raises.
    """
    try:
        ticket = Ticket.model_validate(
            {
                "kind": kind,
                "title": title,
                "body": body,
                "acceptance": list(acceptance),
                "repo": repo,
                "ref": ref,
                "impacted_paths": list(impacted_paths),
            }
        )
    except ValidationError as exc:
        raise InvalidWorkItem(_first_error(exc), hint="factory ticket add --help") from None
    with Trace() as trace:
        store = WorkItems(trace)
        item = store.add(
            ticket,
            parent=parent,
            depends_on=depends_on,
            size=cast("Size | None", size),
            reason=reason,
        )
        return _show(store, item.id, as_json=as_json)


def list_items(status: str | None = None, *, as_json: bool = False) -> int:
    """Print every item, or those in `status`, in id order. Returns 0, even for an empty list.

    Text is one `id<TAB>status<TAB>kind<TAB>title` line per item; JSON is `{"work_items": [...]}`
    with each item's columns.
    """
    with Trace() as trace:
        items = WorkItems(trace).list(status)
    if as_json:
        print(json.dumps({"work_items": [item.to_json() for item in items]}))
    else:
        for item in items:
            print(f"{item.id}\t{item.status}\t{item.kind}\t{item.title}")
    return 0


def show(item_id: str, *, as_json: bool = False) -> int:
    """Print one item with its cost so far and its events. Returns 0.

    Raises `WorkItemNotFound` for an unknown id.
    """
    with Trace() as trace:
        return _show(WorkItems(trace), item_id, as_json=as_json)


def move(item_id: str, to: str, reason: str, *, as_json: bool = False) -> int:
    """Move the item to `to` as actor `human` and print it as `show` does. Returns 0.

    Raises whatever `WorkItems.move` raises for an illegal move, a bad state or an empty reason.
    """
    with Trace() as trace:
        store = WorkItems(trace)
        store.move(item_id, to, reason)
        return _show(store, item_id, as_json=as_json)


def link(item_id: str, pipeline_run_id: str, *, as_json: bool = False) -> int:
    """Link a pipeline run to the item and print it as `show` does. Returns 0.

    Linking a run twice changes nothing. Raises `WorkItemNotFound` for an unknown id.
    """
    with Trace() as trace:
        store = WorkItems(trace)
        store.link_run(item_id, pipeline_run_id)
        return _show(store, item_id, as_json=as_json)


def ready(*, as_json: bool = False) -> int:
    """Promote and block `todo` items as `WorkItems.ready` does, then print what is ready. Returns 0.

    JSON is `{"promoted": [ids], "blocked": [ids], "ready": [items]}`, `ready` in the
    orchestrator's order. Text is a line per promoted and blocked id, then the ready items.
    """
    with Trace() as trace:
        result = WorkItems(trace).ready()
    if as_json:
        print(
            json.dumps(
                {
                    "promoted": list(result.promoted),
                    "blocked": list(result.blocked),
                    "ready": [item.to_json() for item in result.ready],
                }
            )
        )
        return 0
    for item_id in result.promoted:
        print(f"promoted {item_id}")
    for item_id in result.blocked:
        print(f"blocked {item_id}")
    print(f"{len(result.ready)} ready" + (":" if result.ready else ""))
    for item in result.ready:
        print(f"  {item.id}\t{item.kind}\t{item.title}")
    return 0


def _show(store: WorkItems, item_id: str, *, as_json: bool) -> int:
    document = store.document(item_id)
    print(json.dumps(document) if as_json else _text(store.get(item_id), document))
    return 0


def _text(item: WorkItem, document: dict[str, Any]) -> str:
    lines = [
        f"{item.id}  {item.status}  {item.kind}  {item.title}",
        f"repo: {item.repo} @ {item.ref}",
    ]
    if item.parent:
        lines.append(f"parent: {item.parent}")
    if item.depends_on:
        lines.append(f"depends on: {', '.join(item.depends_on)}")
    if item.size:
        lines.append(f"size: {item.size}")
    if item.impacted_paths:
        lines.append(f"impacted paths: {', '.join(item.impacted_paths)}")
    lines.append(f"cost so far: ${document['cost_usd']:.4f}")
    if item.pipeline_run_ids:
        lines.append(f"pipeline runs: {', '.join(item.pipeline_run_ids)}")
    lines.append("acceptance:")
    lines.extend(f"  - {criterion}" for criterion in item.acceptance)
    if item.body:
        lines.extend(["", item.body])
    lines.extend(["", "events:"])
    for event in document["events"]:
        frm = event["from_status"] or "(new)"
        lines.append(
            f"  {event['at']}  {frm} -> {event['to_status']}  {event['actor']}: {event['reason']}"
        )
    return "\n".join(lines)


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error["loc"]) or "ticket"
    return f"{field}: {error['msg']}"
