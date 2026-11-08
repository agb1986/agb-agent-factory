"""Tests for the spike profile of the sandbox: its docker arguments, argv checks and scratch root."""

import shutil
import subprocess
from pathlib import Path

import pytest

import tools.sandbox
from tools import ToolInputError
from tools.fs import list_files
from tools.sandbox import (
    PROFILE_LABEL,
    ROOT_LABEL,
    SANDBOX_IMAGE,
    SANDBOX_IMAGE_ENV,
    SPIKE_EXCLUDES,
    SPIKE_PROGRAMS,
    SPIKE_TIMEOUT_S,
    TIMEOUT_EXIT_CODE,
    check_argv,
    docker_argv,
    run_spike,
    run_spike_command,
    spike_docker_argv,
    spike_root,
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


def _flag(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


# spike_docker_argv


def test_spike_docker_argv_turns_the_network_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SANDBOX_IMAGE_ENV, raising=False)
    argv = spike_docker_argv(tmp_path, ["python", "probe.py"], "name-1")
    assert argv[:3] == ["docker", "run", "--rm"]
    assert _flag(argv, "--network") == ["bridge"]
    assert argv[-3:] == [SANDBOX_IMAGE, "python", "probe.py"]


def test_spike_docker_argv_mounts_only_the_scratch_directory(tmp_path: Path) -> None:
    argv = spike_docker_argv(tmp_path, ["python", "probe.py"], "name-1")
    assert _flag(argv, "--volume") == [f"{tmp_path}:/work:rw"]
    assert ".git" not in " ".join(argv)
    assert _flag(argv, "--workdir") == ["/work"]


def test_spike_docker_argv_keeps_the_offline_limits(tmp_path: Path) -> None:
    spike = spike_docker_argv(tmp_path, ["python"], "name-1")
    offline = docker_argv(tmp_path, ["python"], "name-2")
    for flag in ("--cpus", "--memory", "--pids-limit", "--cap-drop", "--security-opt", "--user"):
        assert _flag(spike, flag) == _flag(offline, flag), flag
    assert "--read-only" in spike


def test_spike_docker_argv_labels_the_root_and_the_profile(tmp_path: Path) -> None:
    argv = spike_docker_argv(tmp_path, ["python"], "name-1")
    assert _flag(argv, "--label") == [f"{ROOT_LABEL}={tmp_path}", f"{PROFILE_LABEL}=spike"]
    assert _flag(argv, "--name") == ["name-1"]


def test_spike_installs_land_in_the_scratch_directory(tmp_path: Path) -> None:
    env = _flag(spike_docker_argv(tmp_path, ["pip", "install", "x"], "name-1"), "--env")
    assert "PIP_USER=1" in env
    assert "PYTHONUSERBASE=/work/.local" in env
    assert "UV_CACHE_DIR=/work/.cache/uv" in env
    assert not [item for item in env if "KEY" in item or "TOKEN" in item]


# check_argv under the spike profile


def test_spike_programs_are_python_pip_uv_and_pytest() -> None:
    assert SPIKE_PROGRAMS == {"pip", "pytest", "python", "uv"}
    for argv in (["pip", "install", "requests"], ["python", "probe.py"], ["uv", "pip", "list"]):
        assert check_argv(argv, programs=SPIKE_PROGRAMS) == argv


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "log"],
        ["curl", "https://httpbin.org"],
        ["sh", "-c", "ls"],
        ["/usr/bin/python"],
        [],
    ],
)
def test_spike_refuses_other_programs(argv: list[str]) -> None:
    with pytest.raises(ToolInputError):
        check_argv(argv, programs=SPIKE_PROGRAMS)


def test_the_offline_programs_still_refuse_pip() -> None:
    with pytest.raises(ToolInputError):
        check_argv(["pip", "install", "requests"])


def test_the_spike_timeout_is_a_hard_limit_under_the_offline_one() -> None:
    assert 0 < SPIKE_TIMEOUT_S <= tools.sandbox.TIMEOUT_S


# spike_root


def test_spike_root_is_an_empty_work_tree_removed_afterwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[Path] = []
    monkeypatch.setattr(tools.sandbox, "kill_sandboxes", lambda root: killed.append(root) or 0)
    with spike_root() as root:
        assert root.is_dir() and root.name.startswith("factory-spike-")
        assert sorted(path.name for path in root.iterdir()) == [".git"]
        exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        for line in SPIKE_EXCLUDES:
            assert line in exclude
        write_file(root, "probe.py", "print(1)\n")
        (root / ".local" / "lib").mkdir(parents=True)
        (root / ".local" / "lib" / "pkg.py").write_text("x = 1\n")
        assert list_files(root).split() == ["probe.py"]
    assert not root.exists()
    assert killed == [root]


def test_spike_root_is_removed_when_the_run_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.sandbox, "kill_sandboxes", lambda _root: 0)
    with pytest.raises(RuntimeError), spike_root() as root:
        raise RuntimeError("boom")
    assert not root.exists()


def test_each_spike_root_is_new(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.sandbox, "kill_sandboxes", lambda _root: 0)
    with spike_root() as first, spike_root() as second:
        assert first != second


def test_run_spike_checks_argv_before_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.sandbox, "kill_sandboxes", lambda _root: 0)
    monkeypatch.setattr(tools.sandbox, "_run_container", lambda *_a: pytest.fail("ran docker"))
    monkeypatch.setattr(tools.sandbox, "require_image", lambda: pytest.fail("checked image"))
    with spike_root() as root, pytest.raises(ToolInputError):
        run_spike(root, ["git", "status"])


# run_spike with docker


@needs_image
def test_run_spike_command_reports_exit_code_and_output() -> None:
    with spike_root() as root:
        out = run_spike_command(root, ["python", "-c", "print('hi')"], max_bytes=65_536)
    assert out == "exit code 0\nhi\n"


@needs_image
def test_spike_writes_only_inside_the_scratch_directory() -> None:
    code = (
        "import pathlib\n"
        "pathlib.Path('/work/out.txt').write_text('ok')\n"
        "try:\n"
        "    pathlib.Path('/etc/x').write_text('no')\n"
        "except OSError as e:\n"
        "    print('refused', type(e).__name__)\n"
    )
    with spike_root() as root:
        result = run_spike(root, ["python", "-c", code])
        assert (root / "out.txt").read_text() == "ok"
    assert result.exit_code == 0
    assert b"refused" in result.output


@needs_image
def test_spike_timeout_kills_the_container() -> None:
    with spike_root() as root:
        result = run_spike(root, ["python", "-c", "import time; time.sleep(60)"], timeout_s=3)
    assert result.exit_code == TIMEOUT_EXIT_CODE
    names = subprocess.run(
        ["docker", "ps", "--filter", "name=factory-sandbox-spike-", "--format", "{{.Names}}"],
        capture_output=True,
        check=False,
        text=True,
    ).stdout
    assert names.strip() == ""
