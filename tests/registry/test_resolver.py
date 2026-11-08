"""Tests for caret ranges, discovery, selection and manifest validation."""

from collections.abc import Callable
from pathlib import Path

import pytest
from packaging.version import Version
from pydantic import BaseModel

from factory.exit import ExitCode, NotFoundError, UsageError, exit_code
from registry import (
    AgentNotFound,
    InvalidRange,
    JevSpec,
    LLMSpec,
    ManifestError,
    VersionNotFound,
    caret_to_specifier,
    list_agents,
    resolve,
)

type MakeAgent = Callable[..., Path]


@pytest.mark.parametrize(
    ("range", "matches", "misses"),
    [
        ("^1.2", ["1.2.0", "1.2.9", "1.9.3"], ["1.1.9", "2.0.0", "0.9.0"]),
        ("^1.0", ["1.0.0", "1.99.0"], ["0.9.9", "2.0.0"]),
        ("^0.3", ["0.3.0", "0.3.7"], ["0.2.9", "0.4.0", "1.0.0"]),
        ("^0.0", ["0.0.1"], ["0.1.0"]),
    ],
)
def test_caret_converts_to_a_specifier(range: str, matches: list[str], misses: list[str]) -> None:
    specifier = caret_to_specifier(range)
    assert all(specifier.contains(Version(v)) for v in matches)
    assert not any(specifier.contains(Version(v)) for v in misses)


def test_caret_never_matches_a_prerelease() -> None:
    assert not caret_to_specifier("^1.2").contains(Version("1.5.0rc1"))


@pytest.mark.parametrize("range", ["1.2", "~1.2", "^1", "^1.2.3", "^01.2", ">=1.2", "", "^1.x"])
def test_malformed_range_is_a_usage_error(range: str) -> None:
    with pytest.raises(InvalidRange) as caught:
        caret_to_specifier(range)
    assert isinstance(caught.value, UsageError)
    assert exit_code(caught.value) == ExitCode.USAGE


def test_resolve_picks_highest_match_across_both_layouts(
    make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent("reviewer", "1.2.0")
    make_agent("reviewer", "1.3.0", subdir=True)
    make_agent("reviewer", "1.9.3", subdir=True)
    make_agent("reviewer", "2.0.0", subdir=True)
    spec = resolve("reviewer", "^1.2", agents_dir)
    assert spec.address == "reviewer@1.9.3"
    assert resolve("reviewer", "^2.0", agents_dir).version == "2.0.0"


def test_resolve_compares_versions_numerically(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.9.0")
    make_agent("reviewer", "1.10.0", subdir=True)
    assert resolve("reviewer", "^1.0", agents_dir).version == "1.10.0"


def test_flat_manifest_wins_when_it_is_the_highest(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.4.0")
    make_agent("reviewer", "1.3.0", subdir=True)
    spec = resolve("reviewer", "^1.0", agents_dir)
    assert spec.version == "1.4.0"
    assert spec.path == agents_dir / "reviewer" / "agent.yaml"


def test_resolve_exposes_attributes_and_loaded_classes(
    make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent("reviewer", "1.2.0")
    spec = resolve("reviewer", "^1.2", agents_dir)
    assert isinstance(spec, LLMSpec)
    assert (spec.model, spec.effort) == ("claude-sonnet-5-5", "high")
    assert spec.tools.allow == ("git.read_file",)
    assert spec.tools.deny == ()
    assert spec.limits.max_tool_turns == 4
    assert spec.limits.timeout_s == 60
    assert spec.limits.max_output_tokens == 1024
    assert spec.evals.min_pass_rate == 0.75
    assert issubclass(spec.input_class, BaseModel)
    assert spec.input_class.__name__ == "In"
    assert spec.output_class.model_validate({"ok": True}).model_dump() == {"ok": True}
    assert spec.system_prompt_file.read_text() == "You review diffs.\n"


def test_sandbox_and_total_token_cap_default_to_off(
    make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent("reviewer", "1.2.0")
    spec = resolve("reviewer", "^1.2", agents_dir)
    assert isinstance(spec, LLMSpec)
    assert spec.sandbox is False
    assert spec.limits.max_tokens_total is None


def test_sandbox_and_total_token_cap_are_read(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent(
        "fixer",
        "1.0.0",
        edit=lambda t: (
            t.replace(
                "  max_output_tokens: 1024\n",
                "  max_output_tokens: 1024\n  max_tokens_total: 9000\n",
            )
            + "sandbox: true\n"
        ),
    )
    spec = resolve("fixer", "^1.0", agents_dir)
    assert isinstance(spec, LLMSpec)
    assert spec.sandbox is True
    assert spec.limits.max_tokens_total == 9000


@pytest.mark.parametrize(
    ("line", "sandbox", "profile"),
    [
        ("", False, None),
        ("sandbox: false\n", False, None),
        ("sandbox: true\n", True, "offline"),
        ("sandbox: spike\n", "spike", "spike"),
    ],
)
def test_each_sandbox_value_names_its_profile(
    line: str,
    sandbox: bool | str,
    profile: str | None,
    make_agent: MakeAgent,
    agents_dir: Path,
) -> None:
    make_agent("fixer", "1.0.0", edit=lambda t: t + line)
    spec = resolve("fixer", "^1.0", agents_dir)
    assert isinstance(spec, LLMSpec)
    assert spec.sandbox == sandbox
    assert type(spec.sandbox) is type(sandbox)
    assert spec.sandbox_profile == profile


@pytest.mark.parametrize("value", ["maybe", "Spike", "offline", "[spike]"])
def test_other_sandbox_values_are_refused(
    value: str, make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent("fixer", "1.0.0", edit=lambda t: t + f"sandbox: {value}\n")
    with pytest.raises(ManifestError, match="sandbox"):
        resolve("fixer", "^1.0", agents_dir)


def test_max_retries_defaults_to_two_and_is_read(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0")
    make_agent(
        "fixer",
        "1.0.0",
        edit=lambda t: t.replace("timeout_s: 60", "timeout_s: 60\n  max_retries: 0"),
    )
    default = resolve("reviewer", "^1.2", agents_dir)
    assert isinstance(default, LLMSpec)
    assert default.limits.max_retries == 2
    none = resolve("fixer", "^1.0", agents_dir)
    assert isinstance(none, LLMSpec)
    assert none.limits.max_retries == 0


def test_unloaded_spec_has_no_classes(make_agent: MakeAgent) -> None:
    path = make_agent("reviewer", "1.2.0")
    spec = resolve("reviewer", "^1.2", path.parent.parent)
    unloaded = LLMSpec.model_validate({**spec.model_dump(), "path": spec.path})
    with pytest.raises(RuntimeError, match="not loaded"):
        unloaded.input_class  # noqa: B018


def test_repeated_resolves_do_not_break_each_other(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0")
    make_agent("other", "1.2.0")
    first = resolve("reviewer", "^1.2", agents_dir)
    resolve("other", "^1.2", agents_dir)
    again = resolve("reviewer", "^1.2", agents_dir)
    assert isinstance(first, LLMSpec) and isinstance(again, LLMSpec)
    assert first.input_class.__name__ == again.input_class.__name__ == "In"


def test_missing_agent_is_not_found(agents_dir: Path) -> None:
    with pytest.raises(AgentNotFound) as caught:
        resolve("nope", "^1.0", agents_dir)
    assert isinstance(caught.value, NotFoundError)
    assert exit_code(caught.value) == ExitCode.NOT_FOUND
    assert caught.value.message == "no agent named 'nope'"
    assert caught.value.hint == "run `factory list`"


def test_missing_agents_directory_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(AgentNotFound):
        resolve("nope", "^1.0", tmp_path / "absent")


def test_no_matching_version_lists_the_available_ones(
    make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent("reviewer", "1.3.0", subdir=True)
    make_agent("reviewer", "1.2.0")
    with pytest.raises(VersionNotFound) as caught:
        resolve("reviewer", "^3.0", agents_dir)
    assert caught.value.message == "no version of reviewer matches ^3.0"
    assert caught.value.available == ("1.2.0", "1.3.0")
    assert caught.value.hint == "available: 1.2.0, 1.3.0"
    assert exit_code(caught.value) == ExitCode.NOT_FOUND


@pytest.mark.parametrize("name", ["../reviewer", "Reviewer", "a_b", ""])
def test_bad_agent_name_is_a_usage_error(name: str, agents_dir: Path) -> None:
    with pytest.raises(UsageError):
        resolve(name, "^1.0", agents_dir)


def test_missing_schema_class_names_file_and_field(make_agent: MakeAgent, agents_dir: Path) -> None:
    path = make_agent(
        "reviewer", "1.2.0", edit=lambda t: t.replace("output_model: Out", "output_model: Gone")
    )
    with pytest.raises(ManifestError) as caught:
        resolve("reviewer", "^1.2", agents_dir)
    assert str(path) in caught.value.message
    assert "output_model: 'Gone' not found" in caught.value.message
    assert exit_code(caught.value) == ExitCode.AUTH_OR_CONFIG


def test_schema_class_that_is_not_a_pydantic_model(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent(
        "reviewer", "1.2.0", edit=lambda t: t.replace("input_model: In", "input_model: NotAModel")
    )
    with pytest.raises(ManifestError, match="input_model: 'NotAModel' .* is not a Pydantic model"):
        resolve("reviewer", "^1.2", agents_dir)


def test_schemas_module_that_fails_to_import(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0", schemas="raise ValueError('boom')\n")
    with pytest.raises(ManifestError, match="schemas: importing .* failed: ValueError: boom"):
        resolve("reviewer", "^1.2", agents_dir)


def test_missing_schemas_and_prompt_files(make_agent: MakeAgent, agents_dir: Path) -> None:
    path = make_agent("reviewer", "1.2.0")
    (path.parent / "schemas.py").unlink()
    with pytest.raises(ManifestError, match="schemas: no such file"):
        resolve("reviewer", "^1.2", agents_dir)
    (path.parent / "system.md").unlink()
    with pytest.raises(ManifestError, match="system_prompt: no such file"):
        resolve("reviewer", "^1.2", agents_dir)


@pytest.mark.parametrize("value", ["../x.py", "/etc/x.py"])
def test_paths_must_stay_inside_the_agent_directory(
    value: str, make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent("reviewer", "1.2.0", edit=lambda t: t.replace("schemas.py", value))
    with pytest.raises(ManifestError, match="schemas: .*inside the agent directory"):
        resolve("reviewer", "^1.2", agents_dir)


def test_unknown_kind(make_agent: MakeAgent, agents_dir: Path) -> None:
    path = make_agent("reviewer", "1.2.0", edit=lambda t: t.replace("kind: llm", "kind: wizard"))
    with pytest.raises(ManifestError) as caught:
        resolve("reviewer", "^1.2", agents_dir)
    assert str(path) in caught.value.message
    assert "kind: unknown kind 'wizard'" in caught.value.message


def test_malformed_yaml(make_agent: MakeAgent, agents_dir: Path) -> None:
    path = make_agent("reviewer", "1.2.0", manifest="name: reviewer\nversion: [1.2\n")
    with pytest.raises(ManifestError) as caught:
        resolve("reviewer", "^1.2", agents_dir)
    assert caught.value.message.startswith(f"{path}: malformed YAML")
    assert "\n" not in caught.value.message


def test_manifest_that_is_not_a_mapping(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0", manifest="- a\n- b\n")
    with pytest.raises(ManifestError, match="must be a mapping"):
        resolve("reviewer", "^1.2", agents_dir)


def test_version_disagreeing_with_the_directory_name(
    make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent(
        "reviewer",
        "1.2.0",
        subdir=True,
        edit=lambda t: t.replace("version: 1.2.0", "version: 1.2.1"),
    )
    with pytest.raises(ManifestError, match="version: '1.2.1' does not match directory '1.2.0'"):
        resolve("reviewer", "^1.0", agents_dir)


def test_name_disagreeing_with_the_directory_name(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0", edit=lambda t: t.replace("name: reviewer", "name: other"))
    with pytest.raises(ManifestError, match="name: 'other' does not match directory 'reviewer'"):
        resolve("reviewer", "^1.0", agents_dir)


def test_same_version_in_both_layouts_is_an_error(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0")
    make_agent("reviewer", "1.2.0", subdir=True)
    with pytest.raises(ManifestError, match="version: 1.2.0 is also at"):
        resolve("reviewer", "^1.0", agents_dir)


@pytest.mark.parametrize("version", ["1.2", "1.2.0rc1", "v1.2.0", "01.2.0"])
def test_version_must_be_major_minor_patch(
    version: str, make_agent: MakeAgent, agents_dir: Path
) -> None:
    make_agent(
        "reviewer", "1.2.0", edit=lambda t: t.replace("version: 1.2.0", f"version: '{version}'")
    )
    with pytest.raises(ManifestError, match="is not MAJOR.MINOR.PATCH"):
        resolve("reviewer", "^1.0", agents_dir)


def test_unknown_key_is_an_error_naming_the_field(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0", edit=lambda t: t + "temperature: 0\n")
    with pytest.raises(ManifestError, match="temperature: Extra inputs are not permitted"):
        resolve("reviewer", "^1.2", agents_dir)


def test_path_is_not_a_manifest_key(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("reviewer", "1.2.0", edit=lambda t: t + "path: elsewhere\n")
    with pytest.raises(ManifestError, match="path: not a manifest key"):
        resolve("reviewer", "^1.2", agents_dir)


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("min_pass_rate: 0.75", "min_pass_rate: 1.5", "evals.min_pass_rate"),
        ("timeout_s: 60", "timeout_s: 0", "limits.timeout_s"),
        ("  max_tool_turns: 4\n", "", "limits.max_tool_turns"),
        ("timeout_s: 60", "timeout_s: 60\n  max_tokens_total: 0", "limits.max_tokens_total"),
        ("timeout_s: 60", "timeout_s: 60\n  max_retries: -1", "limits.max_retries"),
        ("effort: high\n", "effort: high\nsandbox: maybe\n", "sandbox"),
        ("effort: high\n", "", "effort"),
        ("deny: []", "deny: [git.read_file]", "tools"),
        ("allow: [git.read_file]", "allow: [git__read_file]", "tools.allow.0"),
        ("allow: [git.read_file]", "allow: [read_file]", "tools.allow.0"),
        ("name: reviewer", "name: Reviewer", "name"),
    ],
)
def test_invalid_values_name_the_field(
    old: str, new: str, field: str, make_agent: MakeAgent, agents_dir: Path
) -> None:
    path = make_agent("reviewer", "1.2.0", edit=lambda t: t.replace(old, new))
    with pytest.raises(ManifestError) as caught:
        resolve("reviewer", "^1.2", agents_dir)
    assert caught.value.message.startswith(f"{path}: ")
    assert f": {field}" in caught.value.message or f"{path}: {field}" in caught.value.message


def test_jev_manifest_resolves_to_a_typed_jev_spec(agents_dir: Path) -> None:
    (agents_dir / "diff-triage").mkdir()
    path = agents_dir / "diff-triage" / "agent.yaml"
    path.write_text(
        "name: diff-triage\nversion: 0.1.0\nkind: jev\ndescription: Triage.\n"
        "model: jev-1.14.0\n"
        "input: {render: 'x {diff}', max_chars: 10}\n"
        "questions: [{id: q, type: noul, text: 'Why?'}]\n"
        "gates: []\nrouting: []\n"
        "evals: {min_pass_rate: 0.9, min_recall_risky: 0.98}\n"
    )
    spec = resolve("diff-triage", "^0.1", agents_dir)
    assert isinstance(spec, JevSpec)
    assert spec.evals.min_recall_risky == 0.98
    assert [q.id for q in spec.questions] == ["q"]


def test_jev_manifest_still_checks_the_shared_fields(agents_dir: Path) -> None:
    (agents_dir / "diff-triage").mkdir()
    (agents_dir / "diff-triage" / "agent.yaml").write_text(
        "name: diff-triage\nversion: 0.1.0\nkind: jev\nevals: {min_pass_rate: 0.9}\n"
    )
    with pytest.raises(ManifestError, match="description: Field required"):
        resolve("diff-triage", "^0.1", agents_dir)


def test_list_agents_reports_every_version_sorted(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("zeta", "1.0.0")
    make_agent("alpha", "1.10.0", subdir=True)
    make_agent("alpha", "1.2.0")
    (agents_dir / "alpha" / "evals").mkdir()
    (agents_dir / "stray").mkdir()
    (agents_dir / ".gitkeep").write_text("")
    assert [tuple(ref) for ref in list_agents(agents_dir)] == [
        ("alpha", "1.2.0", "llm"),
        ("alpha", "1.10.0", "llm"),
        ("zeta", "1.0.0", "llm"),
    ]


def test_list_agents_without_a_directory_is_empty(tmp_path: Path) -> None:
    assert list_agents(tmp_path / "absent") == []


def test_list_agents_raises_on_a_broken_manifest(make_agent: MakeAgent, agents_dir: Path) -> None:
    make_agent("alpha", "1.0.0")
    make_agent("beta", "1.0.0", manifest="name: [unclosed\n")
    with pytest.raises(ManifestError, match="malformed YAML"):
        list_agents(agents_dir)


def test_default_agents_dir_is_agents_in_the_working_directory(
    make_agent: MakeAgent, agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_agent("alpha", "1.0.0")
    monkeypatch.chdir(agents_dir.parent)
    assert [tuple(r) for r in list_agents()] == [("alpha", "1.0.0", "llm")]
    assert resolve("alpha", "^1.0").address == "alpha@1.0.0"
