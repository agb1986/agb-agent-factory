"""Isolation tests for the fixer prototype's tools and Docker sandbox.

Run them with `uv run pytest prototypes/fixer`; the default `uv run pytest` does not collect this
directory. The tests that start a container are skipped when Docker or the image is missing.
"""

import subprocess
from pathlib import Path

import pytest

from fixtures.build import build
from prototypes.fixer import sandbox
from prototypes.fixer.fixer_tools import MAX_WRITE_BYTES, list_files, read_file, write_file
from prototypes.fixer.sandbox import docker_argv, parse_command, require_image, run_in_sandbox
from tools._common import PathEscapeError, ToolInputError, ToolNotFoundError, ToolUnavailableError


def _sandbox_missing() -> str | None:
    try:
        require_image()
    except ToolUnavailableError as exc:
        return str(exc)
    return None


_MISSING = _sandbox_missing()
needs_sandbox = pytest.mark.skipif(_MISSING is not None, reason=_MISSING or "")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return build(["off-by-one"], dest=tmp_path / "repos")[0].path


@pytest.mark.parametrize(
    "command",
    ["sh -c id", "curl https://example.com", "/usr/bin/python -V", "bash", "pip install x"],
)
def test_parse_command_refuses_programs_outside_the_allow_list(command: str) -> None:
    with pytest.raises(ToolInputError, match="program not allowed"):
        parse_command(command)


@pytest.mark.parametrize("command", ["", "   ", "python -c 'unterminated"])
def test_parse_command_refuses_empty_or_unsplittable_commands(command: str) -> None:
    with pytest.raises(ToolInputError):
        parse_command(command)


def test_parse_command_keeps_shell_syntax_as_plain_arguments() -> None:
    assert parse_command("python -m pytest -q && rm -rf /") == [
        "python",
        "-m",
        "pytest",
        "-q",
        "&&",
        "rm",
        "-rf",
        "/",
    ]


def test_docker_argv_isolates_the_container(tmp_path: Path) -> None:
    argv = docker_argv(tmp_path, ["python", "-V"], "fixer-proto-test")
    joined = " ".join(argv)
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 128",
        "--memory 512m",
        f"--volume {tmp_path}:/work:rw",
        f"--volume {tmp_path}/.git:/work/.git:ro",
    ):
        assert flag in joined
    assert argv[-3:] == [sandbox.IMAGE, "python", "-V"]


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", ".git/config", ".git/hooks/x"])
def test_write_file_refuses_paths_outside_the_work_tree(repo: Path, path: str) -> None:
    with pytest.raises(PathEscapeError):
        write_file(repo, path, "x")
    assert not (repo.parent / "escape.py").exists()


def test_write_file_refuses_a_symlink_out_of_the_root(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "link").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        write_file(repo, "link/planted.py", "x")
    assert list(outside.iterdir()) == []


def test_write_file_refuses_the_root_a_directory_and_oversize_content(repo: Path) -> None:
    with pytest.raises(ToolInputError, match="root"):
        write_file(repo, ".", "x")
    with pytest.raises(ToolInputError, match="directory"):
        write_file(repo, "series", "x")
    with pytest.raises(ToolInputError, match="limit"):
        write_file(repo, "big.txt", "x" * (MAX_WRITE_BYTES + 1))


def test_write_file_creates_parents_and_the_file_reads_back(repo: Path) -> None:
    assert write_file(repo, "tests/new/test_x.py", "assert True\n") == (
        "wrote 12 bytes to tests/new/test_x.py"
    )
    assert read_file(repo, "tests/new/test_x.py") == "assert True\n"
    assert "tests/new/test_x.py" in list_files(repo).splitlines()


def test_read_file_refuses_escapes_and_missing_files(repo: Path) -> None:
    with pytest.raises(PathEscapeError):
        read_file(repo, "../../etc/passwd")
    with pytest.raises(ToolNotFoundError):
        read_file(repo, "series")


@needs_sandbox
def test_sandbox_runs_the_fixture_suite(repo: Path) -> None:
    result = run_in_sandbox(repo, "python -m pytest -q")
    assert result.exit_code == 0, result.output
    assert not list(repo.rglob("__pycache__"))
    assert not (repo / ".pytest_cache").exists()


@needs_sandbox
def test_sandbox_has_no_network(repo: Path) -> None:
    result = run_in_sandbox(
        repo, "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    )
    assert result.exit_code != 0
    assert b"Network is unreachable" in result.output or b"OSError" in result.output


@needs_sandbox
@pytest.mark.parametrize("target", ["/etc/planted", "/usr/local/planted", "/work/../planted"])
def test_sandbox_cannot_write_outside_the_work_tree(repo: Path, target: str) -> None:
    result = run_in_sandbox(repo, f"python -c \"open('{target}', 'w')\"")
    assert result.exit_code != 0
    assert b"Read-only file system" in result.output


@needs_sandbox
def test_sandbox_cannot_change_the_git_directory(repo: Path) -> None:
    config = (repo / ".git" / "config").read_bytes()
    result = run_in_sandbox(repo, "python -c \"open('.git/config', 'a').write('[x]')\"")
    assert result.exit_code != 0
    assert b"Read-only file system" in result.output
    assert (repo / ".git" / "config").read_bytes() == config


@needs_sandbox
def test_sandbox_writes_inside_the_work_tree_as_the_host_user(repo: Path) -> None:
    result = run_in_sandbox(repo, "python -c \"open('made.txt', 'w').write('ok')\"")
    assert result.exit_code == 0, result.output
    made = repo / "made.txt"
    assert made.read_text() == "ok"
    assert made.stat().st_uid == repo.stat().st_uid


@needs_sandbox
def test_sandbox_kills_the_container_on_timeout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "TIMEOUT_S", 3)
    result = run_in_sandbox(repo, 'python -c "import time; time.sleep(60)"')
    assert result.exit_code == 124
    assert result.seconds < 30
    running = subprocess.run(
        ["docker", "ps", "--quiet", "--filter", "name=fixer-proto-"],
        capture_output=True,
        check=True,
    )
    assert running.stdout == b""
