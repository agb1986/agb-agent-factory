"""The registry of scoped, read-only tools and the helpers that turn an agent's lists into wire definitions.

`TOOLS` maps a dotted name (`git.read_file`) to a pair: the tool definition and its implementation.
The definition has `name`, `description` and `input_schema`, and describes only the arguments the
model supplies. The implementation takes the repository root as its first argument, then the
model's arguments, and has a keyword-only `max_bytes`; the runtime calls
`impl(repo_path, **arguments, max_bytes=MAX_OUTPUT_BYTES)` and returns the string to the model.
Every implementation reads only inside the root and raises a `ToolError` subclass on failure.
"""

import copy
from collections.abc import Callable, Iterable
from typing import Any

from tools import git, rg
from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolListError,
    UnknownToolError,
)

MAX_OUTPUT_BYTES = DEFAULT_MAX_BYTES
"""Most bytes one tool call returns to the model. Longer output is cut and ends with a marker."""

_WIRE_SEPARATOR = "__"

type ToolDef = dict[str, Any]
type Tool = tuple[ToolDef, Callable[..., str]]

TOOLS: dict[str, Tool] = {
    "git.read_file": (
        {
            "name": "git.read_file",
            "description": "Return the contents of one file as committed at a git ref.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the repository root."},
                    "ref": {"type": "string", "description": "Commit, branch, tag or other git ref, e.g. HEAD."},
                },
                "required": ["path", "ref"],
                "additionalProperties": False,
            },
        },
        git.read_file,
    ),
    "git.show_diff": (
        {
            "name": "git.show_diff",
            "description": "Return the unified diff from one git ref to another, optionally for one path.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "base_ref": {"type": "string", "description": "The older ref, the diff's left side."},
                    "head_ref": {"type": "string", "description": "The newer ref, the diff's right side."},
                    "path": {"type": "string", "description": "Limit the diff to this path, relative to the repository root."},
                },
                "required": ["base_ref", "head_ref"],
                "additionalProperties": False,
            },
        },
        git.show_diff,
    ),
    "rg.search": (
        {
            "name": "rg.search",
            "description": "Search the repository's files with a ripgrep regex. Returns path:line:text lines.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for."},
                    "glob": {"type": "string", "description": "Only search paths matching this glob, e.g. '*.py'."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
        rg.search,
    ),
}  # fmt: skip


def wire_name(dotted: str) -> str:
    """Return the API tool name for the dotted `dotted`, replacing each dot with `__`.

    Raises `UnknownToolError` when `dotted` is not in `TOOLS`.
    """
    _require_known([dotted])
    return dotted.replace(".", _WIRE_SEPARATOR)


def dotted_name(wire: str) -> str:
    """Return the dotted tool name for the API name `wire`; the inverse of `wire_name`.

    Raises `UnknownToolError` when the result is not in `TOOLS`, which is how the runtime refuses
    a tool name the model made up.
    """
    dotted = wire.replace(_WIRE_SEPARATOR, ".")
    if "." in wire or dotted not in TOOLS:  # a dot never appears in a wire name
        raise _unknown(wire)
    return dotted


def tool_defs(allow: Iterable[str], deny: Iterable[str] = ()) -> list[ToolDef]:
    """Return the wire-format definitions for one agent's `tools.allow` and `tools.deny` lists.

    The result follows the order of `allow`, drops repeats, and names each tool with `__` in place
    of dots. The definitions are copies, so a caller can change them freely. Raises
    `UnknownToolError` for a name in either list that is not in `TOOLS`, and `ToolListError` when
    a name is in both lists.
    """
    allowed = list(dict.fromkeys(allow))
    denied = list(dict.fromkeys(deny))
    _require_known(allowed)
    _require_known(denied)
    overlap = [name for name in allowed if name in denied]
    if overlap:
        raise ToolListError(
            f"tools both allowed and denied: {', '.join(overlap)}",
            hint="remove each from one of tools.allow and tools.deny",
        )
    defs: list[ToolDef] = []
    for name in allowed:
        definition = copy.deepcopy(TOOLS[name][0])
        definition["name"] = wire_name(name)
        defs.append(definition)
    return defs


def _unknown(shown: str) -> UnknownToolError:
    """Return the error for the tool name `shown`, with the known names as its hint."""
    return UnknownToolError(f"unknown tool: {shown}", hint=f"known tools: {', '.join(TOOLS)}")


def _require_known(names: Iterable[str]) -> None:
    """Raise `UnknownToolError` unless every name in `names` is a key of `TOOLS`."""
    unknown = [n for n in names if n not in TOOLS]
    if unknown:
        raise _unknown(", ".join(unknown))
