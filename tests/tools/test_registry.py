"""Tests for the registry, `tool_defs` and the wire-name mapping."""

import re

import pytest

from factory.exit import ExitCode, exit_code
from tools import (
    MAX_OUTPUT_BYTES,
    TOOLS,
    ToolListError,
    UnknownToolError,
    dotted_name,
    tool_defs,
    wire_name,
)
from tools.registry import READ_ONLY, SANDBOX_ONLY

ALL = [
    "git.read_file",
    "git.show_diff",
    "rg.search",
    "fs.list_files",
    "fs.read_file",
    "fs.write_file",
    "shell.run",
    "memory.search",
]
WIRE_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}")  # what the API accepts for a tool name


def test_registry_holds_the_tools() -> None:
    assert list(TOOLS) == ALL
    for name, (definition, impl) in TOOLS.items():
        assert definition["name"] == name
        assert definition["description"]
        assert definition["input_schema"]["type"] == "object"
        assert callable(impl) and impl.__doc__


def test_schemas_describe_only_what_the_model_supplies() -> None:
    for definition, _ in TOOLS.values():
        props = definition["input_schema"]["properties"]
        assert "repo_path" not in props and "max_bytes" not in props
        assert definition["input_schema"]["additionalProperties"] is False
        assert set(definition["input_schema"]["required"]) <= set(props)


def test_output_limit_is_configured_in_the_registry() -> None:
    assert MAX_OUTPUT_BYTES > 0


def test_tool_defs_uses_wire_names_in_allow_order() -> None:
    defs = tool_defs(["rg.search", "git.read_file"], [])
    assert [d["name"] for d in defs] == ["rg__search", "git__read_file"]
    assert all(WIRE_PATTERN.fullmatch(d["name"]) for d in defs)
    assert defs[1]["input_schema"] == TOOLS["git.read_file"][0]["input_schema"]


def test_tool_defs_are_copies() -> None:
    tool_defs(["rg.search"])[0]["input_schema"]["properties"].clear()
    assert TOOLS["rg.search"][0]["input_schema"]["properties"]
    assert TOOLS["rg.search"][0]["name"] == "rg.search"


def test_tool_defs_deny_not_in_allow_is_fine() -> None:
    assert [d["name"] for d in tool_defs(["git.read_file"], ["rg.search"])] == ["git__read_file"]


def test_tool_defs_empty_and_repeated_names() -> None:
    assert tool_defs([], []) == []
    assert len(tool_defs(["rg.search", "rg.search"])) == 1


def test_overlap_raises_and_names_the_tools() -> None:
    with pytest.raises(ToolListError, match="rg.search") as err:
        tool_defs(["git.read_file", "rg.search"], ["rg.search"])
    assert exit_code(err.value) == ExitCode.USAGE


@pytest.mark.parametrize(
    ("allow", "deny"), [(["git.nope"], []), ([], ["nope.nope"]), (["git__read_file"], [])]
)
def test_unknown_name_raises_in_either_list(allow: list[str], deny: list[str]) -> None:
    with pytest.raises(UnknownToolError, match="unknown tool") as err:
        tool_defs(allow, deny)
    assert exit_code(err.value) == ExitCode.NOT_FOUND
    assert err.value.hint and "git.read_file" in err.value.hint


def test_wire_names_round_trip() -> None:
    for name in TOOLS:
        assert dotted_name(wire_name(name)) == name
    assert wire_name("git.read_file") == "git__read_file"
    assert dotted_name("rg__search") == "rg.search"


@pytest.mark.parametrize("wire", ["git__nope", "rg.search", "search", "", "git____read_file"])
def test_dotted_name_refuses_names_the_model_made_up(wire: str) -> None:
    with pytest.raises(UnknownToolError):
        dotted_name(wire)


def test_wire_name_refuses_unknown_dotted_name() -> None:
    with pytest.raises(UnknownToolError):
        wire_name("git.nope")


@pytest.mark.parametrize("name", sorted(SANDBOX_ONLY))
def test_sandbox_only_tools_are_refused_without_the_flag(name: str) -> None:
    with pytest.raises(ToolListError, match=name) as err:
        tool_defs(["fs.read_file", name])
    assert err.value.hint and "sandbox: true" in err.value.hint
    assert exit_code(err.value) == ExitCode.USAGE


def test_sandbox_only_tools_are_handed_out_with_the_flag() -> None:
    defs = tool_defs(["fs.write_file", "shell.run"], sandbox=True)
    assert [d["name"] for d in defs] == ["fs__write_file", "shell__run"]


def test_sandbox_only_tool_that_is_only_denied_is_fine() -> None:
    assert [d["name"] for d in tool_defs(["fs.read_file"], ["shell.run"])] == ["fs__read_file"]


def test_only_the_two_writing_tools_are_sandbox_only() -> None:
    assert SANDBOX_ONLY == {"fs.write_file", "shell.run"}
    assert SANDBOX_ONLY <= set(TOOLS)


def test_every_tool_is_either_read_only_or_sandbox_only() -> None:
    assert READ_ONLY | SANDBOX_ONLY == set(TOOLS)
    assert not READ_ONLY & SANDBOX_ONLY
