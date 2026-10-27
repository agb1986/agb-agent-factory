"""`factory cost`: what the agent runs in the trace cost since a date.

The spend comes from `Trace.cost_breakdown`, so it is the `cost_usd` the runners wrote, run by run.
Runs tagged `eval:` or `publish:` are reported apart from the rest, because an eval or a publish
gate is spend on the agents themselves and not on the work they were built for. `--by agent` groups
by `name@version`, `--by pipeline` by pipeline run id, where a run outside any pipeline appears as
`(none)`, and `--by kind` by runner kind, `claude` or `jev`. Runs whose model has no price carry no
cost and add nothing.
"""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from factory.exit import UsageError
from runtime.trace import CostRow, Trace

BUCKETS: tuple[str, ...] = ("other", "eval", "publish")
"""The tag buckets in report order: untagged and other-tagged runs first."""

_TITLES = {"other": "other runs", "eval": "eval runs", "publish": "publish runs"}
_NO_GROUP = "(none)"
_KEYS = {"agent": "agent", "pipeline": "pipeline_run_id", "kind": "kind"}

By = Literal["agent", "pipeline", "kind"]
"""What `factory cost` groups by."""


def parse_since(value: str) -> datetime:
    """Parse `--since` as an ISO date or datetime; one without an offset is UTC.

    Raises `UsageError` for anything else.
    """
    try:
        since = datetime.fromisoformat(value)
    except ValueError:
        raise UsageError(
            f"--since {value!r} is not a date", hint="use 2026-10-01 or 2026-10-01T09:30:00+00:00"
        ) from None
    return since if since.tzinfo is not None else since.replace(tzinfo=UTC)


def report(rows: list[CostRow], since: datetime, by: By) -> dict[str, Any]:
    """Return the `factory cost` document for `rows`: totals overall and per tag bucket."""
    key = _KEYS[by]
    buckets: dict[str, dict[str, Any]] = {
        name: {"runs": 0, "cost_usd": 0.0, "groups": []} for name in BUCKETS
    }
    for row in rows:
        bucket = buckets[row.bucket]
        bucket["runs"] += row.runs
        bucket["cost_usd"] += row.cost_usd
        bucket["groups"].append(
            {key: row.group or _NO_GROUP, "runs": row.runs, "cost_usd": _round(row.cost_usd)}
        )
    for bucket in buckets.values():
        bucket["cost_usd"] = _round(bucket["cost_usd"])
    return {
        "since": since.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "by": by,
        "total_runs": sum(b["runs"] for b in buckets.values()),
        "total_cost_usd": _round(sum(r.cost_usd for r in rows)),
        "buckets": buckets,
    }


def text(document: dict[str, Any]) -> str:
    """Return `document` (from `report`) as a plain-text table per tag bucket."""
    key = _KEYS[document["by"]]
    lines = [f"cost since {document['since']}, by {document['by']}"]
    for name in BUCKETS:
        bucket = document["buckets"][name]
        if not bucket["groups"]:
            continue
        lines.append("")
        lines.append(f"{_TITLES[name]}: {_runs(bucket['runs'])}, ${bucket['cost_usd']:.4f}")
        width = max(len(group[key]) for group in bucket["groups"])
        for group in bucket["groups"]:
            lines.append(f"  {group[key]:<{width}}  {group['runs']:>5}  ${group['cost_usd']:.4f}")
    lines.append("")
    lines.append(f"total: {_runs(document['total_runs'])}, ${document['total_cost_usd']:.4f}")
    return "\n".join(lines)


def command(since: str, *, by: By = "agent", as_json: bool = False) -> int:
    """Run `factory cost`: print the spend since `since`, as text or one JSON document.

    Returns 0. Raises `UsageError` for a `since` that is not a date.
    """
    start = parse_since(since)
    with Trace() as trace:
        rows = trace.cost_breakdown(start, by)
    document = report(rows, start, by)
    print(json.dumps(document) if as_json else text(document))
    return 0


def _runs(count: int) -> str:
    return f"{count} run" if count == 1 else f"{count} runs"


def _round(cost: float) -> float:
    return round(cost, 6)
