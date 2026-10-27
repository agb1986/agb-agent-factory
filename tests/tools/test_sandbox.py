"""Tests for the sandbox tools: the write tool, argv checks and, when Docker has the image, runs."""

import shutil
import subprocess
from pathlib import Path

import pytest
from repo_helpers import Repo

from tools import PathEscapeError, ToolInputError, ToolUnavailableError
from tools.sandbox import (
    ALLOWED_PROGRAMS,
    MAX_WRITE_BYTES,
    SANDBOX_IMAGE,
    SANDBOX_IMAGE_ENV,
    TIMEOUT_EXIT_CODE,
    check_argv,
    docker_argv,
    require_image,
    run_command,
    run_in_sandbox,
    sandbox_image,
    write_file,
)


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, check=False
    )
    return proc.returncode == 0


needs_image = pytest.mark.skipif(
    not _image_present(), reason=f"docker image {SANDBOX_IMAGE} absent"
)


# write_file


def test_write_file_creates_parents_and_reports_bytes(repo: Repo) -> None:
    result = write_file(repo.root, "pkg/sub/new.py", "x = 'é'\n")
    assert result == "wrote 9 bytes to pkg/sub/new.py"
    assert (repo.root / "pkg" / "sub" / "new.py").read_text() == "x = 'é'\n"


def test_write_file_replaces_an_existing_file(repo: Repo) -> None:
    write_file(repo.root, "src/app.py", "new\n")
    assert (repo.root / "src" / "app.py").read_text() == "new\n"


@pytest.mark.parametrize(
    "path",
    ["../escape.txt", "src/../../escape.txt", "/tmp/escape.txt", "leak.txt", "linkdir/new.txt"],
)
def test_write_file_refuses_paths_outside_the_root(repo: Repo, path: str) -> None:
    with pytest.raises(PathEscapeError):
        write_file(repo.root, path, "x")


def test_write_file_never_writes_through_a_symlink_out(repo: Repo) -> None:
    before = (repo.outside / "secret.txt").read_text()
    with pytest.raises(PathEscapeError):
        write_file(repo.root, "leak.txt", "overwritten")
    assert (repo.outside / "secret.txt").read_text() == before
    assert not (repo.outside / "new.txt").exists()
    with pytest.raises(PathEscapeError):
        write_file(repo.root, "linkdir/new.txt", "x")
    assert not (repo.outside / "new.txt").exists()


@pytest.mark.parametrize("path", [".git/config", ".git/hooks/pre-commit", "src/.git/x", ".git"])
def test_write_file_refuses_anything_under_git(repo: Repo, path: str) -> None:
    before = (repo.root / ".git" / "config").read_text()
    with pytest.raises((PathEscapeError, ToolInputError)):
        write_file(repo.root, path, "x")
    assert (repo.root / ".git" / "config").read_text() == before
    assert not (repo.root / ".git" / "hooks" / "pre-commit").exists()


@pytest.mark.parametrize("path", [".", "", "src"])
def test_write_file_needs_a_file_path(repo: Repo, path: str) -> None:
    with pytest.raises(ToolInputError):
        write_file(repo.root, path, "x")


def test_write_file_size_limit_is_256_kib(repo: Repo) -> None:
    assert MAX_WRITE_BYTES == 256 * 1024
    write_file(repo.root, "big.txt", "a" * MAX_WRITE_BYTES)
    with pytest.raises(ToolInputError, match="limit"):
        write_file(repo.root, "bigger.txt", "a" * (MAX_WRITE_BYTES + 1))
    assert not (repo.root / "bigger.txt").exists()


def test_write_file_limit_counts_bytes_not_characters(repo: Repo) -> None:
    with pytest.raises(ToolInputError):
        write_file(repo.root, "wide.txt", "é" * (MAX_WRITE_BYTES // 2 + 1))


def test_write_file_needs_string_content(repo: Repo) -> None:
    with pytest.raises(ToolInputError):
        write_file(repo.root, "n.txt", 3)  # ty: ignore[invalid-argument-type]


# check_argv


def test_check_argv_accepts_the_allowed_programs() -> None:
    assert check_argv(["python", "-m", "pytest", "-q"]) == ["python", "-m", "pytest", "-q"]
    assert check_argv(("pytest",)) == ["pytest"]
    assert check_argv(["uv", "run", "pytest"]) == ["uv", "run", "pytest"]
    assert check_argv(["git", "diff", "base", "head"]) == ["git", "diff", "base", "head"]
    assert ALLOWED_PROGRAMS == {"pytest", "python", "uv", "git"}


@pytest.mark.parametrize(
    "argv",
    [
        [],
        "python -m pytest",
        None,
        [""],
        ["rm", "-rf", "/"],
        ["sh", "-c", "ls"],
        ["bash"],
        ["/usr/bin/python"],
        ["./python"],
        ["python\0"],
        ["python", 3],
        ["python", "a\0b"],
        ["python"] + ["x"] * 64,
        ["python", "x" * 40_000],
        ["curl", "http://example.com"],
    ],
)
def test_check_argv_refuses(argv: object) -> None:
    with pytest.raises(ToolInputError):
        check_argv(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["git"],
        ["git", "commit", "-m", "x"],
        ["git", "checkout", "base"],
        ["git", "reset", "--hard"],
        ["git", "push"],
        ["git", "config", "user.name", "x"],
        ["git", "-c", "core.pager=sh", "log"],
        ["git", "--git-dir=/etc", "log"],
        ["git", "diff", "--output=/work/x"],
        ["git", "diff", "--ext-diff"],
        ["git", "show", "--textconv", "HEAD:x"],
        ["git", "grep", "-O", "x"],
        ["git", "grep", "--open-files-in-pager=sh", "x"],
    ],
)
def test_check_argv_allows_only_read_only_git(argv: list[str]) -> None:
    with pytest.raises(ToolInputError):
        check_argv(argv)


def test_the_program_hint_lists_the_allowed_programs() -> None:
    with pytest.raises(ToolInputError) as err:
        check_argv(["ls"])
    assert err.value.hint and "pytest" in err.value.hint


# docker_argv


def test_docker_argv_isolates_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SANDBOX_IMAGE_ENV, raising=False)
    argv = docker_argv(tmp_path, ["python", "-m", "pytest"], "name-1")
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network none" in joined
    assert "--read-only" in argv
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert f"{tmp_path}:/work:rw" in joined
    assert f"{tmp_path}/.git:/work/.git:ro" in joined
    assert argv[-4:] == [SANDBOX_IMAGE, "python", "-m", "pytest"][-4:]
    assert argv.index(SANDBOX_IMAGE) < argv.index("python")


@pytest.mark.parametrize("value", [None, "", "  "])
def test_sandbox_image_defaults_without_the_variable(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(SANDBOX_IMAGE_ENV, raising=False)
    else:
        monkeypatch.setenv(SANDBOX_IMAGE_ENV, value)
    assert sandbox_image() == SANDBOX_IMAGE == "factory-sandbox:0.1"


def test_factory_sandbox_image_names_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SANDBOX_IMAGE_ENV, "shop-sandbox:3")
    argv = docker_argv(tmp_path, ["pytest"], "name-1")
    assert argv[-2:] == ["shop-sandbox:3", "pytest"]
    assert SANDBOX_IMAGE not in argv


def test_a_missing_custom_image_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SANDBOX_IMAGE_ENV, "shop-sandbox:3")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ToolUnavailableError, match="shop-sandbox:3") as err:
        require_image()
    assert seen == [["docker", "image", "inspect", "shop-sandbox:3"]]
    assert err.value.hint and SANDBOX_IMAGE_ENV in err.value.hint


# run_in_sandbox without docker


def test_run_in_sandbox_checks_argv_before_docker(repo: Repo) -> None:
    with pytest.raises(ToolInputError):
        run_in_sandbox(repo.root, ["rm", "-rf", "x"])


def test_run_in_sandbox_needs_a_plain_clone(repo: Repo, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    repo.git("worktree", "add", "-q", "--detach", str(linked))
    assert linked.joinpath(".git").is_file()
    with pytest.raises(ToolInputError, match="not a directory") as err:
        run_in_sandbox(linked, ["python", "--version"])
    assert err.value.hint == "use a plain clone"


def test_missing_docker_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(ToolUnavailableError, match="docker"):
        require_image()


def test_missing_image_is_unavailable_with_a_build_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SANDBOX_IMAGE_ENV, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 1, b"", b""),
    )
    with pytest.raises(ToolUnavailableError) as err:
        require_image()
    assert (
        err.value.hint
        and "docker build" in err.value.hint
        and "sandbox.Dockerfile" in err.value.hint
    )


# run_in_sandbox with docker


@needs_image
def test_run_command_reports_exit_code_and_output(repo: Repo) -> None:
    out = run_command(repo.root, ["python", "-c", "print('hi')"], max_bytes=65_536)
    assert out == "exit code 0\nhi\n"
    out = run_command(repo.root, ["python", "-c", "import sys; sys.exit(3)"], max_bytes=65_536)
    assert out.startswith("exit code 3\n")


@needs_image
def test_sandbox_has_no_network(repo: Repo) -> None:
    code = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    result = run_in_sandbox(repo.root, ["python", "-c", code])
    assert result.exit_code != 0


@needs_image
def test_sandbox_cannot_write_to_git_or_outside_the_work_tree(repo: Repo) -> None:
    head = repo.git("rev-parse", "HEAD")
    code = (
        "import pathlib\n"
        "for p in ('/work/.git/hooks/pre-commit', '/etc/x', '/work/ok.txt'):\n"
        "    try:\n"
        "        pathlib.Path(p).write_text('x'); print('wrote', p)\n"
        "    except OSError as e:\n"
        "        print('refused', p)\n"
    )
    result = run_in_sandbox(repo.root, ["python", "-c", code])
    assert result.output.decode().splitlines() == [
        "refused /work/.git/hooks/pre-commit",
        "refused /etc/x",
        "wrote /work/ok.txt",
    ]
    assert repo.git("rev-parse", "HEAD") == head
    assert (repo.root / "ok.txt").exists()


@needs_image
def test_sandbox_runs_read_only_git(repo: Repo) -> None:
    result = run_in_sandbox(repo.root, ["git", "log", "--oneline"])
    assert result.exit_code == 0
    assert "second" in result.output.decode()


@needs_image
def test_sandbox_timeout_kills_the_container(repo: Repo) -> None:
    result = run_in_sandbox(repo.root, ["python", "-c", "import time; time.sleep(60)"], timeout_s=3)
    assert result.exit_code == TIMEOUT_EXIT_CODE
    names = subprocess.run(
        ["docker", "ps", "--filter", "name=factory-sandbox-", "--format", "{{.Names}}"],
        capture_output=True,
        check=False,
        text=True,
    ).stdout
    assert names.strip() == ""
