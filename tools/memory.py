"""Read-only memory tool: search claude-mem and OpenViking through the `agb` command."""

import json
from pathlib import Path

from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolExecutionError,
    ToolInputError,
    ToolSpec,
    ToolUnavailableError,
    fail_hint,
    resolve_root,
    run,
    truncate,
)

DEFAULT_LIMIT = 10
"""Rows returned when the model gives no `limit`; the same default as `agb mem search`."""

MAX_LIMIT = 25
"""Most rows one call may ask for; a larger `limit` is refused rather than silently lowered."""

_AGB_DOWN = 4  # agb's exit code for a backend that is down or not configured


def search(
    repo_path: str | Path,
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Return the memory notes that match `query`, best first, one entry per match.

    Runs `agb --json mem search --backend all --limit N -- QUERY`, which searches claude-mem and
    OpenViking and only reads. Each entry is a line `[backend] ref | timestamp | title` followed
    by an indented snippet line. `backend` is the source (`claude-mem` or `ov`) and `ref` is the
    stable reference to cite: `claude-mem:<observation id>` or the `viking://` URI. `limit` is
    1 to `MAX_LIMIT`; anything else raises `ToolInputError`, as does an empty `query`. No match
    returns an empty string, a backend that is down raises `ToolUnavailableError`, and the output
    is truncated to `max_bytes`. `repo_path` only has to be a git work tree; nothing in it is
    read. The tool reaches the host's memory, so a pipeline must never allow it to an agent with
    `sandbox: true`.
    """
    if not isinstance(query, str) or not query.strip() or "\0" in query:
        raise ToolInputError("query must be a non-empty string without NUL")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ToolInputError(f"limit must be an integer from 1 to {MAX_LIMIT}: {limit!r}")
    root = resolve_root(repo_path)
    argv = ["agb", "--json", "mem", "search", "--backend", "all", "--limit", str(limit), "--"]
    proc = run([*argv, query], root)
    if proc.returncode == _AGB_DOWN:
        raise ToolUnavailableError("memory is not reachable through agb", hint=fail_hint(proc))
    if proc.returncode != 0:
        raise ToolExecutionError(
            f"agb mem search failed with exit code {proc.returncode}", hint=fail_hint(proc)
        )
    try:
        entries = [_entry(row) for row in json.loads(proc.stdout)]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ToolExecutionError(
            "agb mem search returned output that is not a result list"
        ) from exc
    return truncate("".join(entries).encode("utf-8"), max_bytes)


def _entry(row: dict[str, object]) -> str:
    """Return the text of one result row: a header line and an indented snippet line."""
    backend = str(row["backend"])
    ident = str(row["id"])
    ref = f"claude-mem:{ident}" if backend == "claude-mem" else ident
    title = " ".join(str(row.get("title") or "").split())
    snippet = " ".join(str(row.get("snippet") or "").split())
    return f"[{backend}] {ref} | {row.get('timestamp') or 'undated'} | {title}\n  {snippet}\n"


TOOLS = (
    ToolSpec(
        name="memory.search",
        description="Search the machine's long-term memory (claude-mem and OpenViking) and return the best matches, each with its source and a stable ref to cite. Read-only. Reaches the host, so a pipeline must never allow it to an agent with `sandbox: true`.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in plain words."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Most results to return. Default 10."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        impl=search,
        reaches_out=True,
    ),
)  # fmt: skip
"""The tools this module declares; `tools.registry` collects them."""
