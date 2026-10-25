"""Run one allow-listed command inside a throwaway Docker container that sees only the worktree.

Each call is a fresh `docker run --rm` with no network, a read-only root filesystem, capped CPU,
memory and process count, no capabilities and the host user's uid. The worktree is the only
writable mount, and its `.git` directory is mounted again read-only on top, so a command can change
the files under review but not the repository's config or hooks. This is option (a) of
`docs/decisions/0001-fixer-runtime.md`; it is prototype code, not the `pipelines/sandbox.py` of #24.
"""

import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from tools._common import ToolInputError, ToolUnavailableError, resolve_root

IMAGE = "factory-fixer-sandbox:proto"
"""Tag of the image built from this directory's Dockerfile."""

DOCKERFILE_DIR = Path(__file__).parent

ALLOWED_PROGRAMS = frozenset({"python", "pytest"})
"""The only executables a command may start. `python -m pytest` is the usual form."""

TIMEOUT_S = 120
"""Wall-clock limit for one command, container start included."""

_WORKDIR = "/work"


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """What one command did: its argv, exit code, combined output and wall-clock seconds."""

    argv: tuple[str, ...]
    exit_code: int
    output: bytes
    seconds: float


def docker_argv(root: Path, argv: list[str], name: str) -> list[str]:
    """Return the `docker run` argument list that runs `argv` in a container `name` over `root`."""
    git_dir = root / ".git"
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
        "--volume",
        f"{root}:{_WORKDIR}:rw",
        "--volume",
        f"{git_dir}:{_WORKDIR}/.git:ro",
        "--workdir",
        _WORKDIR,
        IMAGE,
        *argv,
    ]


def parse_command(command: str) -> list[str]:
    """Split `command` like a POSIX shell would, without running one, and check its program.

    Raises `ToolInputError` when the command is empty, cannot be split, or starts a program that
    is not in `ALLOWED_PROGRAMS`. A path such as `/usr/bin/python` is refused too: only bare names.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ToolInputError(f"cannot split command: {exc}") from exc
    if not argv:
        raise ToolInputError("command is empty")
    if argv[0] not in ALLOWED_PROGRAMS:
        raise ToolInputError(
            f"program not allowed: {argv[0]!r}",
            hint=f"allowed programs: {', '.join(sorted(ALLOWED_PROGRAMS))}",
        )
    return argv


def run_in_sandbox(repo_path: str | Path, command: str) -> SandboxResult:
    """Run `command` in the sandbox with the work tree at `repo_path` as `/work`.

    `repo_path` must be the top of a git work tree whose `.git` is a directory. A missing Docker
    or image raises `ToolUnavailableError`; a bad command raises `ToolInputError`. A command that
    runs past `TIMEOUT_S` is killed and reported with exit code 124, as `timeout(1)` does. Killing
    the `docker` client does not stop its container, so the container is killed by name.
    """
    root = resolve_root(repo_path)
    argv = parse_command(command)
    if not (root / ".git").is_dir():
        raise ToolInputError(f"{root}/.git is not a directory", hint="use a plain clone")
    require_image()
    name = f"fixer-proto-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    try:
        proc = subprocess.run(
            docker_argv(root, argv, name),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=30, check=False)
        output = (exc.stdout or b"") + (exc.stderr or b"")
        return SandboxResult(tuple(argv), 124, output, time.monotonic() - start)
    return SandboxResult(
        tuple(argv), proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start
    )


def require_image() -> None:
    """Raise `ToolUnavailableError` unless Docker runs and has `IMAGE`."""
    if shutil.which("docker") is None:
        raise ToolUnavailableError("docker is not installed or not on PATH", hint="install docker")
    proc = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30, check=False
    )
    if proc.returncode != 0:
        raise ToolUnavailableError(
            f"sandbox image {IMAGE} is missing",
            hint=f"docker build -t {IMAGE} {DOCKERFILE_DIR}",
        )
