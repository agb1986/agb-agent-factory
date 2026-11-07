"""The runtime refuses web tool calls outside the agent's `tools.web.allow_domains`."""

from collections.abc import Callable
from typing import Any

import pytest

from registry.spec import LLMSpec, ToolsSpec, WebSpec
from runtime.trace import Trace
from tests.runtime.test_runner import (  # noqa: F401
    FakeAPI,
    answer,
    asks_for_tool,
    go,
    make_spec,
    tool_results,
    trace,
)
from tools import TOOLS
from tools._common import ToolListError


def web_spec(make: Callable[..., LLMSpec], domains: tuple[str, ...]) -> LLMSpec:
    tools = ToolsSpec(allow=("web.fetch",), web=WebSpec(allow_domains=domains))
    return make().model_copy(update={"tools": tools})


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(repo_path: Any, url: str, *, max_bytes: int) -> str:
        calls.append(url)
        return "page"

    monkeypatch.setitem(TOOLS, "web.fetch", (TOOLS["web.fetch"][0], fake))
    return calls


def test_a_url_outside_the_allow_list_goes_back_as_an_error_and_never_runs(
    make_spec: Callable[..., LLMSpec],  # noqa: F811
    trace: Trace,  # noqa: F811
    fetched: list[str],
) -> None:
    api = FakeAPI(asks_for_tool(name="web__fetch", url="https://evil.example.com/"), answer())
    go(web_spec(make_spec, ("docs.python.org",)), api, trace)
    [result] = tool_results(api.bodies[1])
    assert result["is_error"] is True
    assert "evil.example.com is not in the allow-list" in result["content"]
    assert fetched == []


def test_a_url_inside_the_allow_list_runs(
    make_spec: Callable[..., LLMSpec],  # noqa: F811
    trace: Trace,  # noqa: F811
    fetched: list[str],
) -> None:
    api = FakeAPI(asks_for_tool(name="web__fetch", url="https://docs.python.org/3/"), answer())
    go(web_spec(make_spec, ("docs.python.org",)), api, trace)
    assert fetched == ["https://docs.python.org/3/"]


def test_an_agent_without_an_allow_list_gets_no_web_tools(
    make_spec: Callable[..., LLMSpec],  # noqa: F811
    trace: Trace,  # noqa: F811
    fetched: list[str],
) -> None:
    api = FakeAPI(answer())
    with pytest.raises(ToolListError, match="allow-list"):
        go(web_spec(make_spec, ()), api, trace)
    assert api.bodies == [] and fetched == []
