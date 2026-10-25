"""Tests for the `factory` console command."""

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from factory.cli import main
from factory.exit import ExitCode


def test_version_prints_package_metadata_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == ExitCode.OK
    assert capsys.readouterr().out.strip() == f"factory {version('agb-agent-factory')}"


def _write_agent(agents: Path, name: str, version: str, *, subdir: bool = False) -> None:
    directory = agents / name / version if subdir else agents / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.yaml").write_text(
        f"name: {name}\nversion: {version}\nkind: jev\ndescription: Test.\n"
        "evals: {min_pass_rate: 0.9}\n"
    )


def test_list_with_no_agents_prints_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["list"]) == ExitCode.OK
    assert capsys.readouterr().out == ""


def test_list_with_no_agents_json_is_an_empty_list(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["list", "--json"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out) == {"agents": []}


def test_list_prints_one_line_per_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_agent(tmp_path / "agents", "beta", "1.0.0")
    _write_agent(tmp_path / "agents", "alpha", "1.3.0", subdir=True)
    _write_agent(tmp_path / "agents", "alpha", "1.2.0")
    monkeypatch.chdir(tmp_path)
    assert main(["list"]) == ExitCode.OK
    assert capsys.readouterr().out.splitlines() == [
        "alpha@1.2.0\tjev",
        "alpha@1.3.0\tjev",
        "beta@1.0.0\tjev",
    ]


def test_list_json_is_one_document_with_name_version_kind(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_agent(tmp_path / "agents", "alpha", "1.2.0")
    monkeypatch.chdir(tmp_path)
    assert main(["list", "--json"]) == ExitCode.OK
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "agents": [{"name": "alpha", "version": "1.2.0", "kind": "jev"}]
    }
    assert captured.err == ""


def test_list_with_a_broken_manifest_exits_4_with_a_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "agents" / "alpha").mkdir(parents=True)
    (tmp_path / "agents" / "alpha" / "agent.yaml").write_text("name: [unclosed\n")
    monkeypatch.chdir(tmp_path)
    assert main(["list"]) == ExitCode.AUTH_OR_CONFIG
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "malformed YAML" in captured.err


def test_list_json_error_prints_the_envelope_on_stderr_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "agents" / "alpha").mkdir(parents=True)
    (tmp_path / "agents" / "alpha" / "agent.yaml").write_text("name: [unclosed\n")
    monkeypatch.chdir(tmp_path)
    assert main(["list", "--json"]) == ExitCode.AUTH_OR_CONFIG
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err.strip().splitlines()[-1])["error"]
    assert error["code"] == 4
    assert "malformed YAML" in error["message"]


def test_no_subcommand_is_usage_error() -> None:
    assert main([]) == ExitCode.USAGE


def test_unknown_subcommand_is_usage_error() -> None:
    assert main(["bogus"]) == ExitCode.USAGE
