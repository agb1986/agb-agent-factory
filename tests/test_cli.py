"""Tests for the `factory` console command."""

from importlib.metadata import version
from pathlib import Path

import pytest

from factory.cli import list_agents, main
from factory.exit import ExitCode


def test_version_prints_package_metadata_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == ExitCode.OK
    assert capsys.readouterr().out.strip() == f"factory {version('agb-agent-factory')}"


def test_list_with_no_agents_prints_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["list"]) == ExitCode.OK
    assert capsys.readouterr().out == ""


def test_list_agents_returns_sorted_dirs_with_manifest(tmp_path: Path) -> None:
    for name in ("zeta", "alpha"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "agent.yaml").write_text("name: x\n")
    (tmp_path / "not-an-agent").mkdir()
    assert list_agents(tmp_path) == ["alpha", "zeta"]


def test_list_agents_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_agents(tmp_path / "nope") == []


def test_no_subcommand_is_usage_error() -> None:
    assert main([]) == ExitCode.USAGE


def test_unknown_subcommand_is_usage_error() -> None:
    assert main(["bogus"]) == ExitCode.USAGE
