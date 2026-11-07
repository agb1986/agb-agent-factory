"""Read-only git tools: file contents at a ref and the diff between two refs.

Both tools read from the object database, never the working tree, and never run a program the
repository configures (no external diff, no textconv).
"""

from pathlib import Path

from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolInputError,
    ToolNotFoundError,
    ToolSpec,
    fail_hint,
    git,
    resolve_commit,
    resolve_inside,
    resolve_root,
    truncate,
)


def read_file(
    repo_path: str | Path, path: str, ref: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> str:
    """Return the contents of the file `path` as committed at `ref`, truncated to `max_bytes`.

    `repo_path` must be the top of a git work tree. `path` is relative to it, and one that escapes
    it raises `PathEscapeError`. A missing ref raises `ToolNotFoundError`, and so does a path that
    is not a file at that ref. A symlink committed at `path` yields its target text, as git stores
    it; it is not followed.
    """
    root = resolve_root(repo_path)
    rel = resolve_inside(root, path)
    if rel == Path("."):
        raise ToolInputError("path must name a file, not the repository root")
    commit = resolve_commit(root, ref)
    proc = git(["cat-file", "blob", f"{commit}:{rel.as_posix()}"], root)
    if proc.returncode != 0:
        raise ToolNotFoundError(f"no file {rel.as_posix()!r} at ref {ref!r}", hint=fail_hint(proc))
    return truncate(proc.stdout, max_bytes)


def show_diff(
    repo_path: str | Path,
    base_ref: str,
    head_ref: str,
    path: str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Return the unified diff from `base_ref` to `head_ref`, truncated to `max_bytes`.

    `path`, when given, limits the diff to that file or directory and is checked like
    `read_file`'s. An empty diff returns an empty string. Unknown refs raise `ToolNotFoundError`.
    """
    root = resolve_root(repo_path)
    rel = None if path is None else resolve_inside(root, path)
    base = resolve_commit(root, base_ref)
    head = resolve_commit(root, head_ref)
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", base, head]
    if rel is not None:
        args += ["--", rel.as_posix()]
    proc = git(args, root)
    if proc.returncode != 0:
        raise ToolNotFoundError(
            f"git diff failed for {base_ref!r}..{head_ref!r}", hint=fail_hint(proc)
        )
    return truncate(proc.stdout, max_bytes)


TOOLS = (
    ToolSpec(
        name="git.read_file",
        description="Return the contents of one file as committed at a git ref.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the repository root."},
                "ref": {"type": "string", "description": "Commit, branch, tag or other git ref, e.g. HEAD."},
            },
            "required": ["path", "ref"],
            "additionalProperties": False,
        },
        impl=read_file,
    ),
    ToolSpec(
        name="git.show_diff",
        description="Return the unified diff from one git ref to another, optionally for one path.",
        input_schema={
            "type": "object",
            "properties": {
                "base_ref": {"type": "string", "description": "The older ref, the diff's left side."},
                "head_ref": {"type": "string", "description": "The newer ref, the diff's right side."},
                "path": {"type": "string", "description": "Limit the diff to this path, relative to the repository root."},
            },
            "required": ["base_ref", "head_ref"],
            "additionalProperties": False,
        },
        impl=show_diff,
    ),
)  # fmt: skip
"""The tools this module declares; `tools.registry` collects them."""
