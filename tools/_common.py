"""Shared plumbing for the tool implementations: errors, repository and path checks, subprocesses.

Every tool in this package is confined to one repository root, and all but the two in `tools.sandbox` are read-only. The helpers here are
the only place that runs a subprocess, resolves a path or truncates output, so the rules in
`docs/CONVENTIONS.md` (Tools) are enforced once rather than once per tool.
"""

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.exit import AuthOrConfigError, FactoryError, NotFoundError, UsageError

DEFAULT_MAX_BYTES = 65_536
"""Default cap on the bytes one tool call returns; see `tools.registry.MAX_OUTPUT_BYTES`."""

TIMEOUT_S = 30
"""Wall-clock limit for one subprocess, in seconds."""

_HINT_CHARS = 200


class ToolError(FactoryError):
    """Base of every failure a tool raises, so the runtime can catch them with one clause."""


class PathEscapeError(ToolError, UsageError):
    """A path leaves the repository root, through `..`, an absolute path or a symlink (exit 2)."""


class ToolInputError(ToolError, UsageError):
    """An argument is malformed: a bad ref, an empty pattern, a root that is not a work tree (exit 2)."""


class ToolNotFoundError(ToolError, NotFoundError):
    """The repository root, ref or file asked for does not exist (exit 3)."""


class ToolUnavailableError(ToolError, AuthOrConfigError):
    """A required executable (`git`, `rg`) is not on PATH (exit 4)."""


class UnknownToolError(ToolError, NotFoundError):
    """A tool name is not in the registry (exit 3)."""


class ToolListError(ToolError, UsageError):
    """An agent's allow and deny lists contradict each other (exit 2)."""


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    """One tool as its module declares it; `tools.registry` collects these from every module.

    `name` is the dotted name the manifests use (`git.read_file`). `description` and
    `input_schema` go to the model as they are, so they describe only the arguments it supplies.
    `impl` follows the registry contract: `impl(repo_path, **arguments, max_bytes=...) -> str`.

    A tool is read-only unless `writes` is true; a tool that writes is sandbox-only, which the
    registry names in `SANDBOX_ONLY`. A tool with `reaches_out` reaches the network or the host,
    so a sandboxed agent never gets it (`SANDBOX_DENIED`). `needs_web_domains` marks a tool that
    also needs the manifest's `tools.web.allow_domains`. A module lists its specs in a module-level
    `TOOLS` tuple, in the order the registry should show them.

    `spike_impl` and `spike_description`, when set, replace `impl` and `description` for an agent
    whose manifest says `sandbox: spike` (`tools.registry.impl_for`): `shell.run` runs its command
    in the networked profile there, and tells the model so.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
    )
    impl: Callable[..., str]
    writes: bool = False
    reaches_out: bool = False
    needs_web_domains: bool = False
    spike_impl: Callable[..., str] | None = None
    spike_description: str | None = None

    def __post_init__(self) -> None:
        if self.writes and self.reaches_out:
            raise ValueError(f"{self.name}: a tool cannot both write and reach out")
        if self.needs_web_domains and not self.reaches_out:
            raise ValueError(f"{self.name}: a tool that needs web domains reaches out")

    def definition(self, *, spike: bool = False) -> dict[str, Any]:
        """Return a fresh wire-format definition: `name`, `description` and `input_schema`.

        With `spike` the description is `spike_description` when the tool has one.
        """
        description = self.description
        if spike and self.spike_description is not None:
            description = self.spike_description
        return {
            "name": self.name,
            "description": description,
            "input_schema": self.input_schema,
        }


class ToolExecutionError(ToolError):
    """A subprocess failed in a way the caller cannot fix by changing arguments (exit 1)."""


def _env() -> dict[str, str]:
    """Return the environment for a subprocess: no inherited git redirection, no pager, no prompts."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C.UTF-8")
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


def _first_line(data: bytes) -> str:
    """Return the first non-empty line of subprocess stderr, shortened for use as a hint."""
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return line.strip()[:_HINT_CHARS]
    return ""


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run `argv` in `cwd` and return the finished process without checking its exit status.

    The command is an argument list and never goes through a shell. A missing executable raises
    `ToolUnavailableError`; running past `TIMEOUT_S` raises `ToolExecutionError`.
    """
    try:
        return subprocess.run(
            argv, cwd=cwd, env=_env(), capture_output=True, timeout=TIMEOUT_S, check=False
        )
    except FileNotFoundError as exc:
        raise ToolUnavailableError(
            f"{argv[0]} is not installed or not on PATH", hint=f"install {argv[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(f"{argv[0]} timed out after {TIMEOUT_S}s") from exc


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run `git` with `args` in `cwd`, with the config that could run other programs switched off."""
    return run(["git", "-c", "core.fsmonitor=false", "--literal-pathspecs", *args], cwd)


def fail_hint(proc: subprocess.CompletedProcess[bytes]) -> str | None:
    """Return the first line of a failed process's stderr as a hint, or None when it is empty."""
    return _first_line(proc.stderr) or None


def resolve_root(repo_path: str | Path) -> Path:
    """Return the real path of `repo_path` after checking it is the top of a git work tree.

    Raises `ToolNotFoundError` when it does not exist and `ToolInputError` when it is not a
    directory that is the top level of a work tree. Every other check resolves against the result.
    """
    path = Path(repo_path)
    if not path.is_dir():
        raise ToolNotFoundError(f"repository root does not exist: {str(repo_path)!r}")
    root = path.resolve()
    proc = git(["rev-parse", "--show-toplevel"], root)
    if proc.returncode != 0:
        raise ToolInputError(f"not a git work tree: {str(repo_path)!r}", hint=fail_hint(proc))
    top = Path(proc.stdout.decode("utf-8", errors="replace").strip()).resolve()
    if top != root:
        raise ToolInputError(
            f"not the top of a git work tree: {str(repo_path)!r}", hint=f"use {top}"
        )
    return root


def resolve_inside(root: Path, path: str) -> Path:
    """Return `path` relative to `root`, after resolving `..` and symlinks against the real disk.

    `root` must come from `resolve_root`. Raises `PathEscapeError`, naming `path`, when the real
    location is outside `root`, whether through `..`, an absolute path or a symlink in any
    component. An absolute path that points inside `root` is accepted. The path need not exist.
    """
    if not isinstance(path, str) or not path or "\0" in path:
        raise ToolInputError(f"path must be a non-empty string without NUL: {path!r}")
    real = (root / path).resolve()
    if not real.is_relative_to(root):
        raise PathEscapeError(f"path escapes the repository root: {path!r}")
    return real.relative_to(root)


def check_ref(ref: str) -> str:
    """Return `ref` unchanged when it is safe to hand to git, else raise `ToolInputError`.

    A ref that starts with `-` would be read as an option, and control characters never appear in
    a ref, so both are refused before any process starts.
    """
    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise ToolInputError(
            f"invalid ref: {ref!r}", hint="a ref is a non-empty name not starting with '-'"
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in ref):
        raise ToolInputError(f"invalid ref: {ref!r}", hint="a ref has no control characters")
    return ref


def resolve_commit(root: Path, ref: str) -> str:
    """Return the full commit id `ref` names in the repository at `root`.

    Raises `ToolInputError` for an unsafe ref and `ToolNotFoundError` when it names no commit.
    """
    proc = git(["rev-parse", "--verify", "--quiet", f"{check_ref(ref)}^{{commit}}"], root)
    if proc.returncode != 0:
        raise ToolNotFoundError(f"no commit named {ref!r}")
    return proc.stdout.decode("ascii", errors="replace").strip()


def truncate(data: bytes, max_bytes: int) -> str:
    """Decode `data` as UTF-8, keeping at most `max_bytes` bytes of it.

    When bytes are dropped, a single final line says so and how many: `[truncated: N bytes dropped,
    limit M bytes]`. The cut never splits a multi-byte character, so the dropped count is exact.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    end = max_bytes
    while end > 0 and data[end] & 0xC0 == 0x80:
        end -= 1
    text = data[:end].decode("utf-8", errors="replace")
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}[truncated: {len(data) - end} bytes dropped, limit {max_bytes} bytes]"
