"""The sandbox-only tools: write a work-tree file, and run one allow-listed command offline.

These are the only tools in the registry that change anything. `tools.registry.tool_defs` hands
them out only to an agent whose manifest says `sandbox: true`. `fs.write_file` writes inside the
repository root and nowhere else. `shell.run` takes an argument list, never a shell string, and
starts only the programs in `ALLOWED_PROGRAMS`, inside a throwaway Docker container that has no
network and sees only the work tree. This is option (a) of `docs/decisions/0001-fixer-runtime.md`.

The container is built from `tools/sandbox.Dockerfile` and tagged `SANDBOX_IMAGE`, or runs the image
named by `FACTORY_SANDBOX_IMAGE` when that is set (`sandbox_image`). It must already hold the
dependencies of the repository under test, because nothing can be installed at run time.
"""

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tools._common import (
    DEFAULT_MAX_BYTES,
    PathEscapeError,
    ToolInputError,
    ToolUnavailableError,
    resolve_inside,
    resolve_root,
    truncate,
)

SANDBOX_IMAGE = "factory-sandbox:0.1"
"""Tag of the image built from `tools/sandbox.Dockerfile`. Configuration: bump it with the file."""

SANDBOX_IMAGE_ENV = "FACTORY_SANDBOX_IMAGE"
"""Environment variable naming a different sandbox image, such as one with a target's dependencies."""

DOCKERFILE = Path(__file__).with_name("sandbox.Dockerfile")

ALLOWED_PROGRAMS = frozenset({"pytest", "python", "uv", "git"})
"""The only executables `shell.run` starts, given as bare names. Git is limited further below."""

GIT_READ_ONLY = frozenset(
    {
        "blame",
        "cat-file",
        "diff",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "rev-list",
        "rev-parse",
        "show",
        "status",
    }
)
"""The git subcommands `shell.run` runs. None of them changes the repository."""

_GIT_REFUSED_OPTIONS = ("--output", "--ext-diff", "--textconv", "--open-files-in-pager", "-O")
"""Git options that write a file or start a program, refused for every subcommand."""

MAX_WRITE_BYTES = 262_144
"""Largest file `fs.write_file` writes, in bytes of UTF-8 (256 KiB)."""

TIMEOUT_S = 120
"""Wall-clock limit for one sandboxed command, container start included."""

TIMEOUT_EXIT_CODE = 124
"""Exit code reported for a command that ran past `TIMEOUT_S`, as `timeout(1)` does."""

_MAX_ARGS = 64
_MAX_ARG_CHARS = 32_768
_WORKDIR = "/work"
_CONTAINER_PREFIX = "factory-sandbox-"


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """What one command did: its argv, exit code, combined output and wall-clock seconds."""

    argv: tuple[str, ...]
    exit_code: int
    output: bytes
    seconds: float


def write_file(
    repo_path: str | Path, path: str, content: str, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> str:
    """Replace or create the work-tree file `path` with `content` and say how many bytes it has.

    Missing parent directories are created. Raises `PathEscapeError` for a path that leaves the
    root, through `..`, an absolute path or a symlink, and for any path with a `.git` component.
    Raises `ToolInputError` for the root itself, a directory, content that is not a string, or
    content over `MAX_WRITE_BYTES`. `max_bytes` is unused: the registry passes it to every tool.
    """
    root = resolve_root(repo_path)
    rel = resolve_inside(root, path)
    if rel == Path("."):
        raise ToolInputError("path must name a file, not the repository root")
    if ".git" in rel.parts:
        raise PathEscapeError(f"path is inside .git: {path!r}")
    if not isinstance(content, str):
        raise ToolInputError("content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolInputError(
            f"content is {len(data)} bytes; the limit is {MAX_WRITE_BYTES}",
            hint="split the change across smaller files",
        )
    target = root / rel
    if target.is_dir():
        raise ToolInputError(f"{rel.as_posix()!r} is a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"wrote {len(data)} bytes to {rel.as_posix()}"


def run_command(
    repo_path: str | Path, argv: Sequence[str], *, max_bytes: int = DEFAULT_MAX_BYTES
) -> str:
    """Run `argv` in the sandbox and return `exit code N`, a newline and the truncated output.

    A non-zero exit is a normal result, not an error: a failing test run is what the agent needs
    to see. Raises `ToolInputError` for an argument list `check_argv` refuses, and
    `ToolUnavailableError` when Docker or the image is missing.
    """
    result = run_in_sandbox(repo_path, argv)
    return f"exit code {result.exit_code}\n{truncate(result.output, max_bytes)}"


def check_argv(argv: object) -> list[str]:
    """Return `argv` as a list of strings after checking it may run, else raise `ToolInputError`.

    It must be a non-empty list of at most 64 strings without NUL. The first is a bare executable
    name from `ALLOWED_PROGRAMS`; a path such as `/usr/bin/python` is refused. For `git` the
    second is a subcommand from `GIT_READ_ONLY`, and no argument is an option that writes a file
    or starts a program.
    """
    if not isinstance(argv, list | tuple) or not argv:
        raise ToolInputError("argv must be a non-empty list of strings")
    if len(argv) > _MAX_ARGS:
        raise ToolInputError(f"argv has {len(argv)} items; the limit is {_MAX_ARGS}")
    if not all(isinstance(item, str) and "\0" not in item for item in argv):
        raise ToolInputError("every argv item must be a string without NUL")
    if any(len(item) > _MAX_ARG_CHARS for item in argv):
        raise ToolInputError(f"an argv item is over {_MAX_ARG_CHARS} characters")
    items = list(argv)
    if items[0] not in ALLOWED_PROGRAMS:
        raise ToolInputError(
            f"program not allowed: {items[0]!r}",
            hint=f"allowed programs: {', '.join(sorted(ALLOWED_PROGRAMS))}",
        )
    if items[0] == "git":
        _check_git(items[1:])
    return items


def _check_git(args: list[str]) -> None:
    if not args or args[0] not in GIT_READ_ONLY:
        raise ToolInputError(
            f"git subcommand not allowed: {args[0] if args else '(none)'!r}",
            hint=f"read-only subcommands: {', '.join(sorted(GIT_READ_ONLY))}; "
            "the subcommand comes first",
        )
    for arg in args:
        if arg.startswith(_GIT_REFUSED_OPTIONS):
            raise ToolInputError(f"git option not allowed: {arg!r}")


def docker_argv(root: Path, argv: Sequence[str], name: str) -> list[str]:
    """Return the `docker run` argument list that runs `argv` in a container `name` over `root`.

    The container has no network, a read-only root filesystem, one CPU, 512 MiB, 128 processes, no
    capabilities and the host user's uid. The work tree is the only writable mount, and its `.git`
    is mounted again read-only on top, so a command can change files but not history or hooks.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--cpus",
        "1",
        "--memory",
        "512m",
        "--pids-limit",
        "128",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTEST_ADDOPTS=-p no:cacheprovider",
        "--env",
        "GIT_OPTIONAL_LOCKS=0",
        "--env",
        "GIT_PAGER=cat",
        "--volume",
        f"{root}:{_WORKDIR}:rw",
        "--volume",
        f"{root / '.git'}:{_WORKDIR}/.git:ro",
        "--workdir",
        _WORKDIR,
        sandbox_image(),
        *argv,
    ]


def run_in_sandbox(
    repo_path: str | Path, argv: Sequence[str], *, timeout_s: int = TIMEOUT_S
) -> SandboxResult:
    """Run `argv` in the sandbox with the work tree at `repo_path` as `/work`.

    `repo_path` must be the top of a git work tree whose `.git` is a directory. A bad argument
    list raises `ToolInputError`; a missing Docker or image raises `ToolUnavailableError`. A
    command that runs past `timeout_s` is killed and reported with `TIMEOUT_EXIT_CODE`. Killing
    the `docker` client does not stop its container, so the container is killed by name.
    """
    root = resolve_root(repo_path)
    items = check_argv(argv)
    if not (root / ".git").is_dir():
        raise ToolInputError(f"{root}/.git is not a directory", hint="use a plain clone")
    require_image()
    name = f"{_CONTAINER_PREFIX}{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    try:
        proc = subprocess.run(
            docker_argv(root, items, name),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=30, check=False)
        output = (exc.stdout or b"") + (exc.stderr or b"")
        return SandboxResult(tuple(items), TIMEOUT_EXIT_CODE, output, time.monotonic() - start)
    return SandboxResult(
        tuple(items), proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start
    )


def sandbox_image() -> str:
    """Return the sandbox image: `FACTORY_SANDBOX_IMAGE` if set and not blank, else `SANDBOX_IMAGE`.

    It is read on every call, so a test or a deployment can change it without a reload. In the
    sibling-container setup of `docs/deploy.md` the name is resolved by the host's Docker daemon.
    """
    return os.environ.get(SANDBOX_IMAGE_ENV, "").strip() or SANDBOX_IMAGE


def require_image() -> None:
    """Raise `ToolUnavailableError` unless Docker runs and has the image `sandbox_image` names."""
    if shutil.which("docker") is None:
        raise ToolUnavailableError("docker is not installed or not on PATH", hint="install docker")
    image = sandbox_image()
    proc = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, timeout=30, check=False
    )
    if proc.returncode != 0:
        hint = f"docker build -t {image} -f {DOCKERFILE} {DOCKERFILE.parent}"
        if image != SANDBOX_IMAGE:
            hint = f"build or pull {image}, or unset {SANDBOX_IMAGE_ENV} to use {SANDBOX_IMAGE}"
        raise ToolUnavailableError(f"sandbox image {image} is missing", hint=hint)
