"""The host-side check of an agent's file policy: the paths its writes may and may not touch.

A sandboxed agent that writes to a repository declares the paths in its manifest's `files` block
(`registry.spec.FilesSpec`). The pipeline running it enforces that block twice, both on the host,
never inside the sandbox where the agent could change the check:

1. `FilePolicyGuard`, the agent's hook, refuses a `fs.write_file` to a denied path before the
   action guard sees it.
2. `check_changed`, called once the agent's run is over, refuses a denied path that changed by
   another route, such as a script run through `shell.run`.

`FilePolicy` adds the paths a pipeline protects for one run on top of the manifest's, each with its
own reason, such as the files the fix loop's test-writer changed. Either refusal goes through the
pipeline's `Refuse` callback, which writes its trace row and returns the error that ends the run.
"""

import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from evals.graders.sandbox_tests import changed_files
from registry.spec import FilesSpec
from runtime.hooks import ToolCall, ToolDecision, ToolResult
from tools._common import ToolError, resolve_inside

GUARDED_TOOL = "fs.write_file"
"""The one tool the hook checks; every other tool is left to the action guard."""

type Refuse = Callable[[str, str, str, str], Exception]
"""`refuse(agent, path, reason, check)`: record the refusal and return the error to raise.

`check` is `hook` for a refusal by `FilePolicyGuard` and `post-check` for one by `check_changed`.
"""


@dataclass(frozen=True, slots=True)
class FilePolicy:
    """The manifest's `files` block plus the paths one run protects, each with its reason."""

    files: FilesSpec
    protected: Mapping[str, str] = field(default_factory=dict)

    def denial(self, path: str) -> str | None:
        """Return why `path`, relative to the repository root, may not be written, or None.

        A protected path's own reason comes first, then the manifest's `FilesSpec.denial`.
        """
        if path in self.protected:
            return self.protected[path]
        return self.files.denial(path)


class FilePolicyGuard:
    """An agent's hook: refuse every `fs.write_file` the policy denies, before the action guard.

    The path is checked as written, after `.` and `..` are folded, and again after symlinks are
    resolved inside `clone`; either match refuses it. A path that does not resolve inside `clone`
    is let through, and `fs.write_file` refuses it itself. A refusal raises what `refuse` returns,
    which ends the agent's run. Every other tool is allowed and left to the action guard.
    """

    def __init__(self, clone: Path, policy: FilePolicy, refuse: Refuse) -> None:
        self.clone = clone
        self.policy = policy
        self.refuse = refuse

    def before_tool(self, call: ToolCall) -> ToolDecision:
        """Raise the refusal for a write the policy denies; allow everything else."""
        if call.name != GUARDED_TOOL:
            return ToolDecision.allow()
        path = call.arguments.get("path")
        if not isinstance(path, str):
            return ToolDecision.allow()
        for candidate in candidates(self.clone, path):
            reason = self.policy.denial(candidate)
            if reason is not None:
                raise self.refuse(call.agent, candidate, reason, "hook")
        return ToolDecision.allow()

    def after_tool(self, call: ToolCall, result: ToolResult) -> None:
        """Do nothing: `check_changed` after the run catches what a hook cannot see."""


def check_changed(clone: Path, policy: FilePolicy, agent: str, refuse: Refuse) -> None:
    """Raise the refusal for the first changed path in `clone`, against HEAD, the policy denies.

    `changed_files` leaves out bytecode and `__pycache__`. Nothing is lost by that as long as the
    caller commits through `pipelines.sandbox.commit_all` and cleans the clone before it runs
    anything, as the fix loop does.
    """
    for path in changed_files(clone):
        reason = policy.denial(path)
        if reason is not None:
            raise refuse(agent, path, reason, "post-check")


def candidates(clone: Path, path: str) -> list[str]:
    """Return `path` relative to `clone` as written and as resolved, without repeats."""
    found: list[str] = []
    lexical = PurePosixPath(posixpath.normpath(path))
    if lexical.is_absolute():
        lexical = (
            lexical.relative_to(clone.as_posix())
            if lexical.is_relative_to(clone.as_posix())
            else PurePosixPath(".")
        )
    if lexical.parts and lexical.parts[0] != "..":
        found.append(lexical.as_posix())
    try:
        resolved = resolve_inside(clone, path).as_posix()
    except ToolError:
        resolved = None
    if resolved is not None and resolved not in found:
        found.append(resolved)
    return [candidate for candidate in found if candidate != "."]
