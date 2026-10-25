"""Read-only work-tree tools: list the files and read one as it is on disk now.

`git.read_file` reads a committed blob, so an agent that edits files cannot see its own edits
through it. These two read the working tree instead. Both are confined to the repository root by
`tools._common.resolve_inside`, so `..`, an absolute path or a symlink out of the root raises
`PathEscapeError` before anything is read.
"""

from pathlib import Path

from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolInputError,
    ToolNotFoundError,
    fail_hint,
    git,
    resolve_inside,
    resolve_root,
    truncate,
)


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
