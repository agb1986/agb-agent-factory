"""Read-only ripgrep tool: search the files of one repository."""

from pathlib import Path

from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolInputError,
    fail_hint,
    resolve_root,
    run,
    truncate,
)


def search(
    repo_path: str | Path,
    pattern: str,
    glob: str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Return the lines under `repo_path` that match the regex `pattern`, as `path:line:text`.

    `glob`, when given, limits the search to matching paths. Results are sorted by path.
    ripgrep honours `.gitignore`, skips hidden files and never follows symlinks, so nothing outside
    the root is read. No match returns an empty string, a bad regex raises `ToolInputError`, and
    the output is truncated to `max_bytes`.
    """
    if not isinstance(pattern, str) or not pattern or "\0" in pattern:
        raise ToolInputError("pattern must be a non-empty string without NUL")
    root = resolve_root(repo_path)
    argv = [
        "rg", "--no-config", "--no-follow", "--color=never", "--no-heading", "--with-filename",
        "--line-number", "--sort=path", "--max-columns=500", "--max-columns-preview",
    ]  # fmt: skip
    if glob is not None:
        if not isinstance(glob, str) or not glob or "\0" in glob:
            raise ToolInputError(f"glob must be a non-empty string without NUL: {glob!r}")
        argv.append(f"--glob={glob}")
    proc = run([*argv, "-e", pattern, "--", "."], root)
    if proc.returncode == 1:
        return ""
    if proc.returncode != 0:
        raise ToolInputError(f"rg rejected the search for {pattern!r}", hint=fail_hint(proc))
    return truncate(proc.stdout.replace(b"\n./", b"\n").removeprefix(b"./"), max_bytes)
