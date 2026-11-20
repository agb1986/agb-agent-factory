"""Tests for the registry, its discovery of each module's tools, `tool_defs` and the wire names."""

import importlib
import pkgutil
import re

import pytest

import tools
import tools.fs
from factory.exit import ExitCode, exit_code
from tools import (
    MAX_OUTPUT_BYTES,
    TOOLS,
    ToolListError,
    UnknownToolError,
    dotted_name,
    registry,
    tool_defs,
    wire_name,
)
from tools._common import ToolSpec
from tools.registry import (
    NEEDS_WEB_DOMAINS,
    OUTSIDE_WRITES,
    READ_ONLY,
    SANDBOX_DENIED,
    SANDBOX_ONLY,
    SPECS,
)

WIRE_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}")  # what the API accepts for a tool name


def _public_modules() -> list[str]:
    """The names of the tools modules the registry discovers, in the order it visits them."""
    names = sorted(m.name for m in pkgutil.iter_modules(tools.__path__))
    return [n for n in names if not n.startswith("_") and n != "registry"]


def _declared() -> list[ToolSpec]:
    """Every module's specs, in discovery order: module name, then declaration order."""
    specs: list[ToolSpec] = []
    for name in _public_modules():
        specs.extend(getattr(importlib.import_module(f"tools.{name}"), "TOOLS", ()))
    return specs


def test_registry_holds_every_declared_tool_in_discovery_order() -> None:
    assert list(TOOLS) == [spec.name for spec in _declared()]
    assert {"git.read_file", "rg.search", "fs.read_file", "fs.write_file", "shell.run"} <= set(
        TOOLS
    )
    for name, (definition, impl) in TOOLS.items():
        assert definition["name"] == name
        assert definition["description"]
        assert definition["input_schema"]["type"] == "object"
        assert callable(impl) and impl.__doc__


def test_every_public_module_declares_tools() -> None:
    for name in _public_modules():
        declared = importlib.import_module(f"tools.{name}").TOOLS
        assert isinstance(declared, tuple) and declared, name
        assert all(isinstance(spec, ToolSpec) for spec in declared), name


def test_tool_names_are_unique_and_prefixed() -> None:
    names = [spec.name for spec in _declared()]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z_]+\.[a-z_]+", name) for name in names)


def test_sets_follow_the_spec_flags() -> None:
    specs = _declared()
    assert SANDBOX_ONLY == {s.name for s in specs if s.writes}
    assert SANDBOX_DENIED == {s.name for s in specs if s.reaches_out}
    assert NEEDS_WEB_DOMAINS == {s.name for s in specs if s.needs_web_domains}
    assert OUTSIDE_WRITES == {s.name for s in specs if s.outside_write}
    assert READ_ONLY == {s.name for s in specs if not s.writes and not s.outside_write}


def test_discovery_refuses_a_duplicate_name(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = SPECS["rg.search"]
    monkeypatch.setattr(tools.fs, "TOOLS", (*tools.fs.TOOLS, spec))
    with pytest.raises(ValueError, match="rg.search is declared twice"):
        registry._discover()


def test_discovery_refuses_a_value_that_is_not_a_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.fs, "TOOLS", (*tools.fs.TOOLS, "fs.oops"))
    with pytest.raises(TypeError, match="tools.fs.TOOLS holds a str"):
        registry._discover()


def test_spec_rejects_contradictory_flags() -> None:
    def impl(repo_path: object, *, max_bytes: int) -> str:
        return ""

    with pytest.raises(ValueError, match="cannot both write and reach out"):
        ToolSpec(name="x.y", description="d", impl=impl, writes=True, reaches_out=True)
    with pytest.raises(ValueError, match="needs web domains reaches out"):
        ToolSpec(name="x.y", description="d", impl=impl, needs_web_domains=True)
    with pytest.raises(ValueError, match="an outside write reaches out"):
        ToolSpec(name="x.y", description="d", impl=impl, outside_write=True)
    with pytest.raises(ValueError, match="does not write inside the sandbox"):
        ToolSpec(name="x.y", description="d", impl=impl, outside_write=True, writes=True)
    spec = ToolSpec(name="x.y", description="d", impl=impl)
    assert spec.definition() == {
        "name": "x.y",
        "description": "d",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


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


def test_the_writing_tools_are_sandbox_only() -> None:
    assert {"fs.write_file", "shell.run"} <= SANDBOX_ONLY
    assert SANDBOX_ONLY <= set(TOOLS)


@pytest.mark.parametrize("name", sorted(SANDBOX_DENIED))
def test_sandbox_denied_tools_are_refused_with_the_flag(name: str) -> None:
    with pytest.raises(ToolListError, match=name) as err:
        tool_defs(["fs.read_file", name], sandbox=True)
    assert exit_code(err.value) == ExitCode.USAGE
    defs = tool_defs([name], web_domains=["example.com"])  # the allow-list the web tools need
    wanted = [] if name in OUTSIDE_WRITES else [name.replace(".", "__")]
    assert [d["name"] for d in defs] == wanted


def test_the_network_tools_are_sandbox_denied() -> None:
    assert {"sentry.list_events", "sentry.read_issue", "memory.search"} <= SANDBOX_DENIED
    assert NEEDS_WEB_DOMAINS == {"web.fetch", "web.search"} <= SANDBOX_DENIED
    assert SANDBOX_DENIED <= READ_ONLY | OUTSIDE_WRITES


def test_every_tool_is_read_only_sandbox_only_or_an_outside_write() -> None:
    assert READ_ONLY | SANDBOX_ONLY | OUTSIDE_WRITES == set(TOOLS)
    assert not READ_ONLY & SANDBOX_ONLY
    assert not READ_ONLY & OUTSIDE_WRITES
    assert not SANDBOX_ONLY & OUTSIDE_WRITES


def test_outside_writes_are_the_github_writes_and_reach_out() -> None:
    assert OUTSIDE_WRITES == {"github.create_pr", "github.comment_pr"}
    assert OUTSIDE_WRITES <= SANDBOX_DENIED
    assert {"github.read_issue", "github.read_pr"} <= READ_ONLY


def test_tool_defs_checks_outside_writes_but_never_hands_one_out() -> None:
    defs = tool_defs(["git.read_file", "github.create_pr", "github.comment_pr", "github.read_pr"])
    assert [d["name"] for d in defs] == ["git__read_file", "github__read_pr"]
    with pytest.raises(ToolListError, match=r"github\.create_pr"):
        tool_defs(["github.create_pr"], ["github.create_pr"])
    with pytest.raises(ToolListError, match=r"github\.comment_pr"):
        tool_defs(["github.comment_pr"], sandbox=True)


# --- the spike profile


def test_the_spike_profile_hands_out_the_sandbox_tools_and_the_web_tools() -> None:
    names = ["fs.write_file", "shell.run", "web.fetch", "web.search"]
    defs = tool_defs(names, sandbox="spike", web_domains=["httpbin.org"])
    assert [d["name"] for d in defs] == [wire_name(name) for name in names]


@pytest.mark.parametrize("name", sorted(SANDBOX_DENIED - NEEDS_WEB_DOMAINS))
def test_the_spike_profile_still_refuses_the_other_network_tools(name: str) -> None:
    with pytest.raises(ToolListError, match=name):
        tool_defs(["fs.read_file", name], sandbox="spike")


def test_the_spike_profile_still_needs_the_web_allow_list() -> None:
    with pytest.raises(ToolListError):
        tool_defs(["web.fetch"], sandbox="spike")


def test_shell_run_has_a_spike_implementation_and_description() -> None:
    spec = SPECS["shell.run"]
    assert spec.spike_impl is not None
    assert registry.impl_for("shell.run", "spike") is spec.spike_impl
    for sandbox in (True, False):
        assert registry.impl_for("shell.run", sandbox) is spec.impl
    (offline,) = tool_defs(["shell.run"], sandbox=True)
    (spike,) = tool_defs(["shell.run"], sandbox="spike")
    assert offline["description"] != spike["description"]
    assert "networked sandbox" in spike["description"]
    assert offline["input_schema"] == spike["input_schema"]


def test_tools_without_a_spike_implementation_keep_their_own() -> None:
    for name, spec in SPECS.items():
        if spec.spike_impl is None:
            assert registry.impl_for(name, "spike") is spec.impl, name
