"""The host side of a sandboxed pipeline: the throwaway checkout, its commits, and the container.

`factory fix` never changes the repository it is pointed at. `require_clean` refuses one with
uncommitted changes, so that the head the agents start from is exactly what was asked for.
`checkout` clones it into a temporary directory and checks out that commit; every agent run, every
sandboxed command and every commit happens in the clone, and the clone is deleted at the end, on an
error too. The source repository gets no commit, ref, hook or config.

`clean` puts the clone back to its HEAD commit before a run that has to see the commits alone,
such as the whole suite: an earlier command can leave untracked or ignored files, bytecode among
them, that the commits do not carry.

`Sandbox` is the seam between the pipeline and Docker: `DockerSandbox` runs commands through
`tools.sandbox.run_in_sandbox` (no network, CPU and memory limits, the work tree as the only
writable mount and its `.git` read-only), and the unit tests pass a fake. Git runs on the host,
through `tools._common.git`, with hooks and fsmonitor off, and the diffs use no external diff
driver or text conversion. The status of the source repository in `require_clean` may still run
the clean filters its owner configured, which are theirs and trusted.
"""

import os
import shutil
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Protocol

from evals.graders.sandbox_tests import changed_files
from factory.exit import UsageError
from registry.work_schemas import FileAction
from tools._common import (
    ToolExecutionError,
    fail_hint,
    git,
    resolve_commit,
    resolve_root,
)
from tools.sandbox import SandboxResult, kill_sandboxes, require_image, run_in_sandbox

SUITE_TIMEOUT_S = 600
"""Wall-clock limit for the whole suite in the sandbox, container start included. Configuration."""

_NO_HOOKS = ["-c", f"core.hooksPath={os.devnull}"]
_COMMITTER = [
    "-c",
    "user.name=factory",
    "-c",
    "user.email=factory@localhost",
    "-c",
    "commit.gpgsign=false",
]
_ACTIONS: dict[str, FileAction] = {"A": "created", "D": "deleted"}


class DirtyRepositoryError(UsageError):
    """The source repository has uncommitted changes, so `factory fix` will not start (exit 2)."""


class Sandbox(Protocol):
    """Where a pipeline runs commands over a work tree. `DockerSandbox` is the real one."""

    def check(self) -> None:
        """Raise `ToolUnavailableError` when commands cannot run at all, before money is spent."""
        ...

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        """Run `argv` over the work tree `root` and return what it did."""
        ...

    def cleanup(self, root: Path) -> None:
        """Stop anything still running over the work tree `root`."""
        ...


class DockerSandbox:
    """The Docker sandbox of `tools.sandbox`, with a longer limit suited to a whole test suite."""

    def __init__(self, timeout_s: int = SUITE_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    def check(self) -> None:
        """Raise `ToolUnavailableError` unless Docker runs and has the sandbox image."""
        require_image()

    def run(self, root: Path, argv: Sequence[str]) -> SandboxResult:
        """Run `argv` with `tools.sandbox.run_in_sandbox`; past `timeout_s` it reports exit 124."""
        return run_in_sandbox(root, argv, timeout_s=self.timeout_s)

    def cleanup(self, root: Path) -> None:
        """Kill every sandbox container still running over `root`."""
        kill_sandboxes(root)


def require_clean(repo_path: str | Path) -> Path:
    """Return the root of the work tree at `repo_path` after checking it has nothing uncommitted.

    Staged, unstaged and untracked changes all count; ignored files do not. Raises
    `DirtyRepositoryError` (exit 2) naming the first few paths, `ToolExecutionError` when git cannot
    report the status, and the `resolve_root` errors for a path that is not a work tree.
    """
    root = resolve_root(repo_path)
    proc = git(["status", "--porcelain=v1", "--untracked-files=normal"], root)
    if proc.returncode != 0:
        raise ToolExecutionError("git status failed in the source repository", hint=fail_hint(proc))
    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    if lines:
        shown = ", ".join(line[3:] for line in lines[:5])
        more = f" and {len(lines) - 5} more" if len(lines) > 5 else ""
        raise DirtyRepositoryError(
            f"{root} has uncommitted changes: {shown}{more}",
            hint="commit or stash them; factory fix starts from a commit and never touches the "
            "work tree",
        )
    return root


@contextmanager
def checkout(root: Path, commit: str, *, work_dir: str | Path | None = None) -> Generator[Path]:
    """Clone the repository at `root` into a new temporary directory and check out `commit`.

    The clone copies the object store (`--no-hardlinks`, so nothing is shared with the source),
    has no hooks and no remote, and its HEAD is detached at `commit`. It lives under `work_dir`
    (default: the system temporary directory) and is deleted when the block ends, however it ends.
    Yields the clone's resolved root. Raises `ToolExecutionError` when git cannot clone or check
    out; the clone runs under git's `tools._common.TIMEOUT_S`, so a very large repository fails
    with a timeout rather than hanging.
    """
    parent = Path(tempfile.mkdtemp(prefix="factory-fix-", dir=work_dir))
    try:
        clone = parent / "repo"
        proc = git(
            [
                *_NO_HOOKS,
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--template=",
                "--config",
                f"core.hooksPath={os.devnull}",
                "--config",
                "core.fsmonitor=false",
                "--",
                str(root),
                str(clone),
            ],
            parent,
        )
        if proc.returncode != 0:
            raise ToolExecutionError(f"git clone of {root} failed", hint=fail_hint(proc))
        clone = clone.resolve()
        proc = git([*_NO_HOOKS, "checkout", "--quiet", "--detach", commit], clone)
        if proc.returncode != 0:
            raise ToolExecutionError(f"git checkout of {commit} failed", hint=fail_hint(proc))
        proc = git(["remote", "remove", "origin"], clone)
        if proc.returncode != 0:
            raise ToolExecutionError("git remote remove failed", hint=fail_hint(proc))
        yield clone
    finally:
        _remove(parent)


def clean(clone: Path) -> None:
    """Reset the work tree of `clone` to its HEAD commit, so a run over it sees the commits alone.

    Changes to tracked files are undone, and every untracked and ignored file is deleted, such as
    bytecode in a `__pycache__` or an ignored module that an earlier command left and that would
    change what a test run imports. Raises `ToolExecutionError` when git fails or something is
    left that could not be deleted.
    """
    proc = git([*_NO_HOOKS, "reset", "--hard", "--quiet", "HEAD"], clone)
    if proc.returncode != 0:
        raise ToolExecutionError("git reset failed in the checkout", hint=fail_hint(proc))
    # `git clean` exits 1 when it cannot delete a file, such as one in a read-only directory; the
    # status below says what is left.
    removed = git(["clean", "-ffdxq"], clone)
    proc = git(["status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=all"], clone)
    if proc.returncode != 0:
        raise ToolExecutionError("git status failed in the checkout", hint=fail_hint(proc))
    left = [
        entry[3:] for entry in proc.stdout.decode("utf-8", errors="replace").split("\0") if entry
    ]
    if left:
        shown = ", ".join(left[:5])
        more = f" and {len(left) - 5} more" if len(left) > 5 else ""
        raise ToolExecutionError(
            f"the checkout could not be cleaned: {shown}{more}", hint=fail_hint(removed)
        )


def _remove(path: Path) -> None:
    """Delete the directory tree at `path`, read-only directories in it included, and never raise.

    A command in the sandbox runs as the user and can take the write bit off a directory it made,
    which stops `shutil.rmtree`. The second pass gives every directory, symbolic links aside, its
    owner's permissions back first; whatever still cannot be deleted is left.
    """
    try:
        shutil.rmtree(path)
    except OSError:
        pass
    else:
        return
    with suppress(OSError):
        path.chmod(0o700)
    for top, dirs, _ in os.walk(path):
        for name in dirs:
            sub = os.path.join(top, name)
            if not os.path.islink(sub):
                with suppress(OSError):
                    os.chmod(sub, 0o700)
    shutil.rmtree(path, ignore_errors=True)


def commit_all(clone: Path, message: str) -> str:
    """Commit everything that changed in the work tree of `clone` and return the new commit id.

    New, changed and deleted files are staged, except the caches `changed_files` leaves out. The
    commit is made even when nothing changed, so each step of a pipeline has its own commit. Hooks
    and signing are off and the identity is fixed. Raises `ToolExecutionError` when git fails.
    """
    paths = changed_files(clone)
    if paths:
        proc = git(["add", "--all", "--", *paths], clone)
        if proc.returncode != 0:
            raise ToolExecutionError("git add failed in the checkout", hint=fail_hint(proc))
    proc = git(
        [
            *_NO_HOOKS,
            *_COMMITTER,
            "commit",
            "--quiet",
            "--no-verify",
            "--allow-empty",
            "--message",
            message,
        ],
        clone,
    )
    if proc.returncode != 0:
        raise ToolExecutionError("git commit failed in the checkout", hint=fail_hint(proc))
    return resolve_commit(clone, "HEAD")


def changed_between(clone: Path, base: str, head: str) -> dict[str, FileAction]:
    """Return what changed between the commits `base` and `head` of `clone`: path to action.

    The paths are in sorted order. A rename is a deletion and an addition, so both paths appear,
    and a change of file type counts as `modified`. Raises `ToolExecutionError` when git fails.
    """
    proc = git(["diff", "--name-status", "--no-renames", "-z", base, head, "--"], clone)
    if proc.returncode != 0:
        raise ToolExecutionError("git diff failed in the checkout", hint=fail_hint(proc))
    fields = proc.stdout.decode("utf-8", errors="replace").split("\0")
    changes = {
        path: _ACTIONS.get(status, "modified")
        for status, path in zip(fields[0::2], fields[1::2], strict=False)
        if path
    }
    return dict(sorted(changes.items()))


def diff_patch(clone: Path, base: str, head: str) -> bytes:
    """Return the change from `base` to `head` of `clone` as a patch that `git apply` takes.

    External diff drivers, text conversion, colour and rename detection are off, the prefixes are
    `a/` and `b/`, and binary files are included in full, so the patch is the same whatever the
    user's git config says and applies with `git apply` from the repository root.
    """
    proc = git(
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--binary",
            "--full-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
            head,
            "--",
        ],
        clone,
    )
    if proc.returncode != 0:
        raise ToolExecutionError(f"git diff {base}..{head} failed", hint=fail_hint(proc))
    return proc.stdout
