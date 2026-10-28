"""`factory trace`: export one pipeline run as an Agent Flow event log.

Agent Flow (https://github.com/patoles/agent-flow) draws agent orchestration from a JSONL file with
one `{time, type, payload, sessionId}` event per line, `time` in seconds from the start of the
session. A run hangs under the run its `parent_run_id` names (the test-writer above the fixer, a
guarded run above its guard decisions); a run with no parent, or one whose parent is not in this
pipeline run, hangs under a `pipeline` root, and runs that overlap in time show as parallel. A
run's decision, error and token counts ride along as a message and a context update on that child.
Replay only: the export reads rows that are already written.
"""

import json
from collections.abc import Iterator
from typing import Any

from factory.exit import UsageError
from runtime.trace import Trace, TraceRow

ROOT = "pipeline"


def events(
    pipeline_run_id: str, rows: list[TraceRow], *, stretch: float = 1.0
) -> Iterator[dict[str, Any]]:
    """Yield the Agent Flow events for the runs of one pipeline invocation, in time order.

    `stretch` multiplies every event time, so a run that took half a second can be played slowly.
    """
    if stretch <= 0:
        raise UsageError("--stretch must be greater than 0")
    if not rows:
        raise UsageError(
            f"no runs for pipeline run {pipeline_run_id!r}",
            hint="list ids with: factory cost --since 2026-01-01 --by pipeline",
        )
    t0 = min(row.started_at for row in rows)
    end = max(row.started_at.timestamp() + row.duration_ms / 1000 for row in rows) - t0.timestamp()

    def event(time: float, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "time": round(time * stretch, 3),
            "type": type_,
            "payload": payload,
            "sessionId": pipeline_run_id,
        }

    yield event(0.0, "agent_spawn", {"name": ROOT, "isMain": True, "task": pipeline_run_id})
    names = {row.run_id: f"{row.agent_ref} #{row.run_id[:6]}" for row in rows}
    timeline: list[dict[str, Any]] = []
    for row in rows:
        parent = names.get(row.parent_run_id or "", ROOT)
        start = (row.started_at - t0).total_seconds()
        finish = start + row.duration_ms / 1000
        name = names[row.run_id]
        timeline.append(
            event(
                start,
                "subagent_dispatch",
                {"parent": parent, "child": name, "task": row.tag or row.kind},
            )
        )
        timeline.append(
            event(
                start, "agent_spawn", {"name": name, "parent": parent, "task": row.tag or row.kind}
            )
        )
        if row.input_tokens is not None or row.output_tokens is not None:
            used = (row.input_tokens or 0) + (row.output_tokens or 0)
            timeline.append(
                event(
                    finish,
                    "context_update",
                    {
                        "agent": name,
                        "tokens": used,
                        "breakdown": {
                            "systemPrompt": 0,
                            "userMessages": row.input_tokens or 0,
                            "toolResults": 0,
                            "reasoning": row.output_tokens or 0,
                            "subagentResults": 0,
                        },
                    },
                )
            )
        note = row.error or (
            json.dumps(row.decision_json) if row.decision_json is not None else None
        )
        if note:
            timeline.append(event(finish, "message", {"agent": name, "content": note}))
        if row.error:
            timeline.append(event(finish, "error", {"agent": name, "message": row.error}))
        cost = f"${row.cost_usd:.4f}" if row.cost_usd is not None else "no price"
        timeline.append(
            event(finish, "subagent_return", {"child": name, "parent": parent, "summary": cost})
        )
        timeline.append(event(finish, "agent_complete", {"name": name}))
    yield from sorted(timeline, key=lambda e: e["time"])
    yield event(end, "agent_complete", {"name": ROOT, "isMain": True})


def command(pipeline_run_id: str, *, out: str | None, stretch: float = 1.0) -> int:
    """Write the event log for `pipeline_run_id` to `out`, or to stdout when `out` is None."""
    with Trace() as trace:
        lines = [
            json.dumps(e)
            for e in events(pipeline_run_id, trace.pipeline_runs(pipeline_run_id), stretch=stretch)
        ]
    text = "\n".join(lines) + "\n"
    if out is None:
        print(text, end="")
    else:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)
    return 0
