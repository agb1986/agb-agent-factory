"""Tests for the `team` label and the `files` policy in agent manifests (registry/spec.py)."""

import typing
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from registry import AgentRef, JevSpec, LLMSpec, ManifestError, list_agents, resolve
from registry.spec import TEAMS, FilesSpec, Team
from runtime.trace import Trace

type MakeAgent = Callable[..., Path]

REPO_AGENTS = Path(__file__).parents[2] / "agents"

ADR_TEAMS: dict[str, str | None] = {
    "product-architect": "planning",
    "story-breakdown": "planning",
    "fixer": "dev",
    "feature-coder": "dev",
    "refactorer": "dev",
    "code-reviewer": "qa",
    "security-reviewer": "qa",
    "perf-reviewer": "qa",
    "judge": "qa",
    "test-writer": "qa",
    "triage-diagnostics": "sre",
    "diff-triage": "sre",
    "judge-gate": "sre",
    "technical-feasibility": "research",
    "benchmark": "research",
    "spike": "research",
    "code-archaeology": "decomp",
    "extraction-modularization": "decomp",
    "action-guard": None,
}
"""Each agent's team, from the table in docs/decisions/0002-work-items.md. Spike and story-breakdown,
added after the table, join their research and planning siblings."""


def _with(text: str, extra: str) -> Callable[[str], str]:
    return lambda manifest: manifest.replace("effort: high\n", f"effort: high\n{text}{extra}")


# --- team


def test_the_teams_tuple_lists_the_literal() -> None:
    assert typing.get_args(Team.__value__) == TEAMS


def test_team_defaults_to_none_and_is_read(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("alpha", "1.0.0")
    make_agent("beta", "1.0.0", edit=lambda t: t + "team: qa\n")
    assert resolve("alpha", "^1.0", agents_dir).team is None
    assert resolve("beta", "^1.0", agents_dir).team == "qa"


@pytest.mark.parametrize("team", TEAMS)
def test_every_team_is_accepted(team: str, make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("alpha", "1.0.0", edit=lambda t: t + f"team: {team}\n")
    assert resolve("alpha", "^1.0", agents_dir).team == team


@pytest.mark.parametrize("team", ["ops", "QA", "''"])
def test_an_unknown_team_is_an_error_naming_the_field(
    team: str, make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent("alpha", "1.0.0", edit=lambda t: t + f"team: {team}\n")
    with pytest.raises(ManifestError, match="team"):
        resolve("alpha", "^1.0", agents_dir)


def _repo_spec(ref: AgentRef) -> LLMSpec | JevSpec:
    major, minor, _ = ref.version.split(".")
    return resolve(ref.name, f"^{major}.{minor}", REPO_AGENTS)


def test_every_agent_in_the_repository_has_its_adr_team() -> None:
    specs = {ref.name: _repo_spec(ref) for ref in list_agents(REPO_AGENTS)}
    found = {name: spec.team for name, spec in specs.items()}
    assert {name: found[name] for name in ADR_TEAMS} == ADR_TEAMS
    assert isinstance(specs["diff-triage"], JevSpec)  # a Jev manifest carries a team too


def test_a_team_change_keeps_the_published_pin(
    make_agent: MakeAgent, agents_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "traces.duckdb"
    monkeypatch.setenv("FACTORY_TRACE_DB", str(store))
    make_agent("alpha", "1.0.0")
    with Trace(store) as trace:
        trace.record_published("alpha", "1.0.0", 1.0, "sha")
    make_agent("alpha", "1.0.0", edit=lambda t: t + "team: dev\n")
    spec = resolve("alpha", "^1.0", agents_dir, published_only=True, trace_path=store)
    assert (spec.version, spec.team) == ("1.0.0", "dev")


# --- files


def test_files_is_read_and_defaults_to_none(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("alpha", "1.0.0", edit=_with("sandbox: true\n", ""))
    make_agent(
        "beta",
        "1.0.0",
        edit=_with("sandbox: true\n", "files:\n  allow: ['src/**']\n  deny: ['src/vendor/**']\n"),
    )
    alpha = resolve("alpha", "^1.0", agents_dir)
    beta = resolve("beta", "^1.0", agents_dir)
    assert isinstance(alpha, LLMSpec) and isinstance(beta, LLMSpec)
    assert alpha.files is None
    assert beta.files == FilesSpec(allow=("src/**",), deny=("src/vendor/**",))


def test_files_without_a_sandbox_is_an_error(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("alpha", "1.0.0", edit=_with("", "files:\n  deny: ['tests/**']\n"))
    with pytest.raises(ManifestError, match="only an agent with a sandbox writes files"):
        resolve("alpha", "^1.0", agents_dir)


@pytest.mark.parametrize(
    "pattern",
    ["", " tests/**", "tests\\\\x", "/etc/**", "../outside/**", "a/../b", "./tests/**", "a//b"],
)
def test_a_bad_glob_is_refused(pattern: str) -> None:
    with pytest.raises(ValidationError):
        FilesSpec(deny=(pattern,))


def test_a_repeated_glob_and_one_in_both_lists_are_refused() -> None:
    with pytest.raises(ValidationError, match="listed twice"):
        FilesSpec(deny=("tests/**", "tests/**"))
    with pytest.raises(ValidationError, match="in both allow and deny"):
        FilesSpec(allow=("tests/**",), deny=("tests/**",))


@pytest.mark.parametrize(
    ("path", "denied"),
    [
        ("tests/test_x.py", True),
        ("a/tests/b/c.py", True),
        ("tests", False),
        ("pkg/conftest.py", True),
        ("conftest.py", True),
        ("src/app.py", False),
        ("pytest.ini", True),
        ("series/pytest.ini", False),
    ],
)
def test_denial_matches_whole_paths(path: str, denied: bool) -> None:
    files = FilesSpec(deny=("**/tests/**", "**/conftest.py", "pytest.ini"))
    assert (files.denial(path) is not None) is denied


def test_an_empty_allow_list_allows_what_deny_does_not_name() -> None:
    assert FilesSpec().denial("anything/at/all.py") is None
    assert FilesSpec(allow=("src/**",)).denial("docs/x.md") == (
        "the path matches no files.allow pattern"
    )


def test_the_fixer_denies_tests_and_the_test_run_and_allows_code() -> None:
    fixer = resolve("fixer", "^0.1", REPO_AGENTS)
    assert isinstance(fixer, LLMSpec) and fixer.files is not None
    for path in ("tests/test_x.py", "conftest.py", "pyproject.toml", "pytest/__init__.py"):
        assert fixer.files.denial(path) is not None, path
    for path in ("series/rolling.py", "series/pyproject.toml", "pytest.md", "py.typed"):
        assert fixer.files.denial(path) is None, path
