"""The registry of scoped tools and the helpers that turn an agent's lists into wire definitions.

The registry is discovered, not written by hand. Every public module in this package (`tools.git`,
`tools.fs`, ...) ends with a `TOOLS` tuple of `ToolSpec` values, and importing this module collects
them in module name order, then in each module's declared order. A new tool is added by appending a
`ToolSpec` to its module's tuple; nothing in this file changes.

`TOOLS` maps a dotted name (`git.read_file`) to a pair: the tool definition and its implementation.
The definition has `name`, `description` and `input_schema`, and describes only the arguments the
model supplies. The implementation takes the repository root as its first argument, then the
model's arguments, and has a keyword-only `max_bytes`; the runtime calls
`impl(repo_path, **arguments, max_bytes=MAX_OUTPUT_BYTES)` and returns the string to the model.
Every implementation acts only inside the root and raises a `ToolError` subclass on failure.

The sets `READ_ONLY`, `SANDBOX_ONLY` and `SANDBOX_DENIED` come from the specs' flags. A tool with
`writes=True` is in `SANDBOX_ONLY` and `tool_defs` refuses it unless the agent's manifest sets
`sandbox` (`true` or `spike`); every other tool is in `READ_ONLY`. A tool with `reaches_out=True` is
in `SANDBOX_DENIED` and `tool_defs` refuses it to a sandboxed agent, with one exception: under
`sandbox: spike`, whose container is networked anyway, the web tools (`NEEDS_WEB_DOMAINS`) stay
available, still bound to the manifest's `tools.web.allow_domains`. The other tools that reach out,
such as the memory and Sentry ones, are refused under both profiles.

Under `sandbox: spike` a tool may also have a second implementation and description
(`ToolSpec.spike_impl`, `spike_description`); `impl_for` and `tool_defs` pick them. `shell.run` is
the one tool with them: its command runs in the networked profile of `tools.sandbox`.
"""

import copy
import importlib
import pkgutil
from collections.abc import Callable, Iterable
from typing import Any

import tools
from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolListError,
    ToolSpec,
    UnknownToolError,
)

MAX_OUTPUT_BYTES = DEFAULT_MAX_BYTES
"""Most bytes one tool call returns to the model. Longer output is cut and ends with a marker."""

_WIRE_SEPARATOR = "__"

type ToolDef = dict[str, Any]
type Tool = tuple[ToolDef, Callable[..., str]]


def _discover() -> dict[str, ToolSpec]:
    """Return the specs of every public module in `tools`, keyed by tool name.

    Modules are visited in name order and each module's `TOOLS` tuple in its declared order. A
    module whose name starts with an underscore, and this module, declare no tools. Raises
    `TypeError` when a module's `TOOLS` holds something other than `ToolSpec` values and
    `ValueError` when two modules declare the same tool name.
    """
    specs: dict[str, ToolSpec] = {}
    for info in sorted(pkgutil.iter_modules(tools.__path__), key=lambda m: m.name):
        if info.name.startswith("_") or info.name == "registry":
            continue
        module = importlib.import_module(f"tools.{info.name}")
        declared = getattr(module, "TOOLS", ())
        for spec in declared:
            if not isinstance(spec, ToolSpec):
                raise TypeError(
                    f"tools.{info.name}.TOOLS holds a {type(spec).__name__}, not a ToolSpec"
                )
            if spec.name in specs:
                raise ValueError(
                    f"tool {spec.name} is declared twice, the second time in tools.{info.name}"
                )
            specs[spec.name] = spec
    return specs


SPECS: dict[str, ToolSpec] = _discover()
"""Every tool's spec, keyed by dotted name, in discovery order."""

TOOLS: dict[str, Tool] = {name: (spec.definition(), spec.impl) for name, spec in SPECS.items()}
"""Every tool's definition and implementation, keyed by dotted name. Prototypes may add to it."""

SANDBOX_ONLY: frozenset[str] = frozenset(name for name, spec in SPECS.items() if spec.writes)
"""The tools that change files or start programs; only an agent with `sandbox` set gets them."""

SANDBOX_DENIED: frozenset[str] = frozenset(name for name, spec in SPECS.items() if spec.reaches_out)
"""The tools that reach the network or the host; a sandboxed agent never gets them, except the web
tools under `sandbox: spike` (see the module docstring)."""

NEEDS_WEB_DOMAINS: frozenset[str] = frozenset(
    name for name, spec in SPECS.items() if spec.needs_web_domains
)
"""The tools that only work with a `tools.web.allow_domains` list in the manifest."""

READ_ONLY: frozenset[str] = frozenset(name for name, spec in SPECS.items() if not spec.writes)
"""Names of the tools in `TOOLS` that change nothing. The action guard (`runtime.guard`) skips them.

A tool that is not listed here, and a name that is not in `TOOLS` at all, counts as one that writes.
A tool that writes is declared with `writes=True` and so left out of this set.
"""


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


def impl_for(dotted: str, sandbox: bool | str = False) -> Callable[..., str]:
    """Return the implementation of the tool `dotted` for an agent with the manifest's `sandbox`.

    Under `sandbox: spike` it is the spec's `spike_impl` when it has one; otherwise, and for a name
    a prototype added to `TOOLS` without a spec, it is the implementation in `TOOLS`.
    """
    spec = SPECS.get(dotted)
    if sandbox == "spike" and spec is not None and spec.spike_impl is not None:
        return spec.spike_impl
    return TOOLS[dotted][1]


def tool_defs(
    allow: Iterable[str],
    deny: Iterable[str] = (),
    *,
    sandbox: bool | str = False,
    web_domains: Iterable[str] = (),
) -> list[ToolDef]:
    """Return the wire-format definitions for one agent's `tools.allow` and `tools.deny` lists.

    The result follows the order of `allow`, drops repeats, and names each tool with `__` in place
    of dots. The definitions are copies, so a caller can change them freely. Raises
    `UnknownToolError` for a name in either list that is not in `TOOLS`, and `ToolListError` when
    a name is in both lists, when `allow` names a `SANDBOX_ONLY` tool and `sandbox` is false, a
    `SANDBOX_DENIED` tool and `sandbox` is set (except a web tool under `spike`), or a
    `NEEDS_WEB_DOMAINS` tool and `web_domains` is empty. `sandbox` is the manifest's `sandbox`
    value (`False`, `True` or `"spike"`) and `web_domains` its `tools.web.allow_domains` list.
    Under `spike` a tool with a `spike_description` is described by it.
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
    refused = [name for name in allowed if name in SANDBOX_ONLY]
    if refused and not sandbox:
        raise ToolListError(
            f"sandbox-only tools need a sandboxed agent: {', '.join(refused)}",
            hint="set `sandbox: true` in the manifest or remove them from tools.allow",
        )
    spike = sandbox == "spike"
    barred = [
        name
        for name in allowed
        if name in SANDBOX_DENIED and not (spike and name in NEEDS_WEB_DOMAINS)
    ]
    if barred and sandbox:
        raise ToolListError(
            f"tools not available inside the sandbox: {', '.join(barred)}",
            hint="remove them from tools.allow or set `sandbox: false` in the manifest",
        )
    unlisted = [name for name in allowed if name in NEEDS_WEB_DOMAINS]
    if unlisted and not list(web_domains):
        raise ToolListError(
            f"web tools need an allow-list: {', '.join(unlisted)}",
            hint="set tools.web.allow_domains in the manifest or remove them from tools.allow",
        )
    defs: list[ToolDef] = []
    for name in allowed:
        spec = SPECS.get(name)
        if spike and spec is not None:
            definition = copy.deepcopy(spec.definition(spike=True))
        else:
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
