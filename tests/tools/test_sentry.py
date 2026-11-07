"""Tests for the Sentry tools, on recorded responses served through `httpx.MockTransport`."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from factory.exit import ExitCode, exit_code
from tools import TOOLS, ToolExecutionError, ToolInputError, ToolNotFoundError, sentry
from tools.sentry import ToolConfigError

FIXTURES = Path(__file__).parent / "fixtures" / "sentry"
TOKEN = "sntrys_not_a_real_token_0123456789"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Recorder:
    """A transport that serves the recorded responses and remembers each request."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        path = request.url.path
        if path == "/api/0/projects/acme/shop-backend/events/":
            return httpx.Response(200, json=fixture("project_events"))
        if path == "/api/0/projects/acme/other/events/":
            return httpx.Response(200, json=[])
        if path == "/api/0/issues/4501/":
            return httpx.Response(200, json=fixture("issue"))
        if path == "/api/0/issues/4501/events/latest/":
            return httpx.Response(200, json=fixture("latest_event"))
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("SENTRY_ORG", "acme")
    monkeypatch.setenv("SENTRY_PROJECT", "shop-backend")
    monkeypatch.delenv("SENTRY_BASE_URL", raising=False)


def test_list_events_filters_by_since_and_normalises(tmp_path: Path) -> None:
    rec = Recorder()
    out = json.loads(sentry.list_events(tmp_path, "2026-11-01T00:00:00Z", transport=rec.transport))
    assert out["project"] == "shop-backend"
    assert [e["issue_id"] for e in out["events"]] == ["4501", "4502"]
    assert out["events"][0] == {
        "id": "7f3c1a9e0b2d4c5e8a6b1d2c3e4f5a6b",
        "issue_id": "4501",
        "title": "KeyError: 'plan_id'",
        "time": "2026-11-05T09:14:22Z",
        "release": "shop@2.8.1",
        "environment": "production",
    }
    request = rec.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.url.host == "sentry.io"


def test_list_events_project_argument_overrides_the_environment(tmp_path: Path) -> None:
    rec = Recorder()
    out = json.loads(sentry.list_events(tmp_path, "2026-01-01", "other", transport=rec.transport))
    assert out == {"project": "other", "events": []}
    assert rec.requests[0].url.path == "/api/0/projects/acme/other/events/"


def test_base_url_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTRY_BASE_URL", "https://sentry.example.test/")
    rec = Recorder()
    sentry.list_events(tmp_path, "2026-01-01", transport=rec.transport)
    assert rec.requests[0].url.host == "sentry.example.test"


def test_read_issue_returns_trace_release_breadcrumbs_and_counts(tmp_path: Path) -> None:
    out = json.loads(sentry.read_issue(tmp_path, "4501", transport=Recorder().transport))
    assert out["title"] == "KeyError: 'plan_id'"
    assert out["count"] == 342
    assert out["user_count"] == 87
    assert out["release"] == "shop@2.8.1"
    assert out["first_release"] == "shop@2.7.9"
    assert out["exceptions"][0]["type"] == "KeyError"
    frames = out["exceptions"][0]["frames"]
    assert frames[-1] == {
        "file": "billing/subscriptions.py",
        "function": "apply_plan",
        "line": 17,
        "in_app": True,
    }
    assert [b["category"] for b in out["breadcrumbs"]] == ["httplib", "query", "auth"]
    assert "context" not in json.dumps(out)


def test_output_is_compact_json_and_capped(tmp_path: Path) -> None:
    text = sentry.read_issue(tmp_path, "4501", transport=Recorder().transport)
    assert "\n" not in text and text.startswith('{"id":"4501"')
    capped = sentry.read_issue(tmp_path, "4501", max_bytes=200, transport=Recorder().transport)
    assert capped.endswith("limit 200 bytes]")
    assert len(capped.encode()) < 300


def test_frames_and_breadcrumbs_are_limited(tmp_path: Path) -> None:
    event = fixture("latest_event")
    frame = {"filename": "f.py", "function": "f", "lineNo": 1, "inApp": True}
    crumb = {"category": "c", "level": "info", "timestamp": "t", "message": "m"}
    event["entries"][0]["data"]["values"] = [crumb] * 100
    event["entries"][1]["data"]["values"][0]["stacktrace"]["frames"] = [frame] * 100

    def handle(request: httpx.Request) -> httpx.Response:
        body = fixture("issue") if request.url.path == "/api/0/issues/4501/" else event
        return httpx.Response(200, json=body)

    out = json.loads(sentry.read_issue(tmp_path, "4501", transport=httpx.MockTransport(handle)))
    assert len(out["breadcrumbs"]) == sentry.MAX_BREADCRUMBS
    assert len(out["exceptions"][0]["frames"]) == sentry.MAX_FRAMES


@pytest.mark.parametrize("call", ["list", "read"])
def test_missing_token_is_a_typed_error_that_names_the_variable(
    call: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTRY_AUTH_TOKEN")
    rec = Recorder()
    with pytest.raises(ToolConfigError, match="SENTRY_AUTH_TOKEN") as err:
        if call == "list":
            sentry.list_events(tmp_path, "2026-01-01", transport=rec.transport)
        else:
            sentry.read_issue(tmp_path, "4501", transport=rec.transport)
    assert exit_code(err.value) == ExitCode.AUTH_OR_CONFIG
    assert rec.requests == []


def test_missing_org_or_project_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTRY_PROJECT")
    with pytest.raises(ToolConfigError, match="SENTRY_PROJECT"):
        sentry.list_events(tmp_path, "2026-01-01", transport=Recorder().transport)
    monkeypatch.delenv("SENTRY_ORG")
    with pytest.raises(ToolConfigError, match="SENTRY_ORG"):
        sentry.list_events(tmp_path, "2026-01-01", "p", transport=Recorder().transport)


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_token_is_a_config_error_without_the_token(status: int, tmp_path: Path) -> None:
    with pytest.raises(ToolConfigError) as err:
        sentry.read_issue(tmp_path, "4501", transport=Recorder(status).transport)
    assert TOKEN not in str(err.value) and TOKEN not in (err.value.hint or "")


def test_unknown_issue_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(ToolNotFoundError):
        sentry.read_issue(tmp_path, "9999", transport=Recorder().transport)


def test_server_error_and_network_failure_are_execution_errors(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="500"):
        sentry.read_issue(tmp_path, "4501", transport=Recorder(500).transport)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect with {TOKEN}")

    with pytest.raises(ToolExecutionError) as err:
        sentry.read_issue(tmp_path, "4501", transport=httpx.MockTransport(boom))
    assert TOKEN not in str(err.value)


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "4501?x=1"])
def test_issue_id_must_be_one_path_segment(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ToolInputError):
        sentry.read_issue(tmp_path, bad, transport=Recorder().transport)


def test_since_must_be_a_time(tmp_path: Path) -> None:
    with pytest.raises(ToolInputError, match="since"):
        sentry.list_events(tmp_path, "yesterday", transport=Recorder().transport)


def test_registry_entries_match_the_implementations() -> None:
    for name, impl in [
        ("sentry.list_events", sentry.list_events),
        ("sentry.read_issue", sentry.read_issue),
    ]:
        definition, registered = TOOLS[name]
        assert registered is impl
        assert "repo_path" not in definition["input_schema"]["properties"]
        assert "max_bytes" not in definition["input_schema"]["properties"]
        assert "transport" not in definition["input_schema"]["properties"]
