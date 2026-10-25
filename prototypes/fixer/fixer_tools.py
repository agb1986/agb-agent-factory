"""The fixer prototype's tools: read, list and write worktree files, and run a sandboxed command.

They follow the `tools.registry` contract, `impl(repo_path, **arguments, max_bytes=...) -> str`,
and `register()` adds them to `tools.registry.TOOLS` at run time so the unchanged
`runtime.runner.run` can serve them. Unlike the read-only registry tools they act on the working
tree, not on a commit, and two of them change it. Every path goes through
`tools._common.resolve_inside`, so `..`, an absolute path or a symlink out of the root raises
`PathEscapeError` before anything is read or written. Prototype code only; #22 owns the real ones.
"""

from pathlib import Path

from prototypes.fixer.sandbox import ALLOWED_PROGRAMS, run_in_sandbox
from tools._common import (
    DEFAULT_MAX_BYTES,
    PathEscapeError,
    ToolInputError,
    ToolNotFoundError,
    fail_hint,
    git,
    resolve_inside,
    resolve_root,
    truncate,
)
from tools.registry import TOOLS, Tool

MAX_WRITE_BYTES = 262_144
"""Largest file `fs.write_file` writes, in bytes of UTF-8."""


def list_files(repo_path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Return the work tree's tracked and untracked, not ignored, files, one path per line."""
    root = resolve_root(repo_path)
    proc = git(["ls-files", "--cached", "--others", "--exclude-standard"], root)
    if proc.returncode != 0:
        raise ToolInputError("git ls-files failed", hint=fail_hint(proc))
    return truncate(proc.stdout, max_bytes)


def read_file(repo_path: str | Path, path: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Return the current contents of the work-tree file `path`, truncated to `max_bytes`.

    Raises `PathEscapeError` for a path outside the root and `ToolNotFoundError` for a path that
    is not a regular file.
    """
    root = resolve_root(repo_path)
    rel = resolve_inside(root, path)
    target = root / rel
    if not target.is_file():
        raise ToolNotFoundError(f"no file {rel.as_posix()!r} in the work tree")
    return truncate(target.read_bytes(), max_bytes)


def write_file(
    repo_path: str | Path, path: str, content: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> str:
    """Replace or create the work-tree file `path` with `content` and say how many bytes it has.

    Missing parent directories are created. Raises `PathEscapeError` for a path outside the root
    or inside `.git`, and `ToolInputError` for the root itself, a directory, or content over
    `MAX_WRITE_BYTES`. `max_bytes` is unused; the registry contract passes it to every tool.
    """
    root = resolve_root(repo_path)
    rel = resolve_inside(root, path)
    if rel == Path("."):
        raise ToolInputError("path must name a file, not the repository root")
    if rel.parts[0] == ".git":
        raise PathEscapeError(f"path is inside .git: {path!r}")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolInputError(f"content is {len(data)} bytes; the limit is {MAX_WRITE_BYTES}")
    target = root / rel
    if target.is_dir():
        raise ToolInputError(f"{rel.as_posix()!r} is a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"wrote {len(data)} bytes to {rel.as_posix()}"


def run_command(repo_path: str | Path, command: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Run `command` in the sandbox and return its exit code and combined output.

    A non-zero exit is a normal result, not an error: a failing test run is what the fixer needs
    to see. Raises `ToolInputError` for a program outside `sandbox.ALLOWED_PROGRAMS`.
    """
    result = run_in_sandbox(repo_path, command)
    return f"exit code {result.exit_code}\n{truncate(result.output, max_bytes)}"


_PATH = {"type": "string", "description": "File path relative to the repository root."}

PROTOTYPE_TOOLS: dict[str, Tool] = {
    "fs.list_files": (
        {
            "name": "fs.list_files",
            "description": "List the work tree's files, tracked and untracked, one path per line.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        list_files,
    ),
    "fs.read_file": (
        {
            "name": "fs.read_file",
            "description": "Return the current contents of one file in the work tree.",
            "input_schema": {
                "type": "object",
                "properties": {"path": _PATH},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        read_file,
    ),
    "fs.write_file": (
        {
            "name": "fs.write_file",
            "description": "Create or replace one file in the work tree with the given text.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "content": {"type": "string", "description": "The file's complete new text."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
        write_file,
    ),
    "shell.run": (
        {
            "name": "shell.run",
            "description": (
                "Run one command in an offline sandbox whose working directory is the repository "
                f"root. Allowed programs: {', '.join(sorted(ALLOWED_PROGRAMS))}. No shell syntax: "
                "no pipes, redirects or &&. Returns the exit code and output."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command line, e.g. 'python -m pytest -q'.",
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        run_command,
    ),
}


def register() -> None:
    """Add the prototype tools to `tools.registry.TOOLS`; calling it again changes nothing."""
    TOOLS.update(PROTOTYPE_TOOLS)
