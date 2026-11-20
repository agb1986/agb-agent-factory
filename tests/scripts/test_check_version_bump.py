"""Tests for scripts/check_version_bump.py, run against throwaway git repositories."""

import importlib.util
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_version_bump.py"

MANIFEST = """\
name: {name}
version: {version}
kind: llm
system_prompt: system.md
schemas: schemas.py
{extra}"""
QUESTIONS = "questions:\n  - id: q1\n    type: noul\n    text: Is it fine?\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_version_bump", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _write(
    repo: Path, name: str, version: str, *, prompt: str = "v1", subdir: bool = False
) -> Path:
    directory = repo / "agents" / name / (version if subdir else "")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.yaml").write_text(
        MANIFEST.format(name=name, version=version, extra=QUESTIONS)
    )
    (directory / "system.md").write_text(prompt)
    (directory / "schemas.py").write_text("class In: ...\n")
    return directory


def _commit(repo: Path, message: str = "change") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    _write(path, "alpha", "1.0.0")
    _commit(path, "base")
    _git(path, "checkout", "-q", "-b", "work")
    return path


def _violations(repo: Path, base: str = "main") -> dict[str, tuple[str, ...]]:
    return {v.directory: v.changed for v in script.check(repo, base)}


def test_unchanged_agent_passes(repo: Path) -> None:
    assert _violations(repo) == {}


def test_changed_prompt_without_a_bump_fails(repo: Path) -> None:
    _write(repo, "alpha", "1.0.0", prompt="v2")
    assert _violations(repo) == {"agents/alpha": ("system prompt",)}


def test_changed_prompt_with_a_bump_passes(repo: Path) -> None:
    _write(repo, "alpha", "1.1.0", prompt="v2")
    assert _violations(repo) == {}


def test_committed_change_is_found_against_the_merge_base(repo: Path) -> None:
    _write(repo, "alpha", "1.0.0", prompt="v2")
    _commit(repo)
    assert _violations(repo) == {"agents/alpha": ("system prompt",)}


def test_changed_schemas_without_a_bump_fails(repo: Path) -> None:
    (repo / "agents/alpha/schemas.py").write_text("class In: ...\nclass Out: ...\n")
    assert _violations(repo) == {"agents/alpha": ("schemas",)}


def test_changed_questions_without_a_bump_fails(repo: Path) -> None:
    manifest = repo / "agents/alpha/agent.yaml"
    manifest.write_text(manifest.read_text().replace("Is it fine?", "Is it good?"))
    assert _violations(repo) == {"agents/alpha": ("questions",)}


def test_a_change_outside_the_guarded_content_needs_no_bump(repo: Path) -> None:
    manifest = repo / "agents/alpha/agent.yaml"
    manifest.write_text(manifest.read_text() + "description: Reworded.\n")
    assert _violations(repo) == {}


@pytest.mark.parametrize(
    "addition",
    ["team: dev\n", "files:\n  deny: ['tests/**']\n", "team: qa\nfiles:\n  allow: ['src/**']\n"],
)
def test_a_team_or_files_change_needs_no_bump(repo: Path, addition: str) -> None:
    # The team is a label and the files policy is enforced by the pipeline around the agent;
    # neither changes what the model is asked or how its answer is read (docs/CONVENTIONS.md).
    manifest = repo / "agents/alpha/agent.yaml"
    manifest.write_text(manifest.read_text() + addition)
    _commit(repo)
    assert _violations(repo) == {}


def test_every_offending_change_is_listed(repo: Path) -> None:
    _write(repo, "beta", "1.0.0")
    _commit(repo, "add beta")
    (repo / "agents/alpha/system.md").write_text("v2")
    (repo / "agents/alpha/schemas.py").write_text("changed\n")
    (repo / "agents/beta/system.md").write_text("v2")
    assert _violations(repo, "work") == {
        "agents/alpha": ("system prompt", "schemas"),
        "agents/beta": ("system prompt",),
    }


def test_a_new_agent_counts_as_a_bump(repo: Path) -> None:
    _write(repo, "beta", "1.0.0", prompt="anything")
    assert _violations(repo) == {}


def test_a_new_version_directory_counts_as_a_bump(repo: Path) -> None:
    _write(repo, "alpha", "1.1.0", prompt="v2", subdir=True)
    assert _violations(repo) == {}


def test_versioned_layout_is_checked(repo: Path) -> None:
    _write(repo, "gamma", "2.0.0", subdir=True)
    _commit(repo, "add gamma")
    _write(repo, "gamma", "2.0.0", prompt="v2", subdir=True)
    assert _violations(repo, "work") == {"agents/gamma/2.0.0": ("system prompt",)}


def test_manifest_without_prompt_or_schemas_files_is_compared_on_questions(repo: Path) -> None:
    jev = repo / "agents" / "jev"
    jev.mkdir()
    (jev / "agent.yaml").write_text("name: jev\nversion: 1.0.0\nkind: jev\n" + QUESTIONS)
    _commit(repo, "add jev")
    (jev / "agent.yaml").write_text(
        "name: jev\nversion: 1.0.0\nkind: jev\n" + QUESTIONS.replace("fine", "ok")
    )
    assert _violations(repo, "work") == {"agents/jev": ("questions",)}


def test_unknown_base_is_a_script_error(repo: Path) -> None:
    with pytest.raises(script.ScriptError, match="merge-base"):
        script.check(repo, "no-such-ref")


# --- the command line


@pytest.fixture
def run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., int]:
    monkeypatch.chdir(repo)
    return script.main


def test_main_exit_0_when_clean(run: Callable[..., int]) -> None:
    assert run(["main"]) == 0


def test_main_exit_1_names_the_agent(
    repo: Path, run: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "alpha", "1.0.0", prompt="v2")
    assert run(["main"]) == 1
    err = capsys.readouterr().err
    assert "agents/alpha" in err
    assert "system prompt" in err
    assert "version is still 1.0.0" in err


def test_main_passes_after_the_bump(repo: Path, run: Callable[..., int]) -> None:
    _write(repo, "alpha", "1.0.1", prompt="v2")
    assert run(["main"]) == 0


def test_main_exit_2_for_an_unknown_base(
    run: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["no-such-ref"]) == 2
    assert "check_version_bump:" in capsys.readouterr().err


def test_main_exit_2_for_too_many_arguments(run: Callable[..., int]) -> None:
    assert run(["main", "extra"]) == 2


def test_main_exit_2_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert script.main(["main"]) == 2
