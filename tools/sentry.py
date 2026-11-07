"""Read-only Sentry telemetry tools: list recent events and read one issue.

Both tools call the Sentry REST API with `httpx` and return a compact JSON string, so a diagnostics
agent gets stack traces, the release tag, breadcrumbs and counts without the raw payload. They read
no file in the repository, so `repo_path` is accepted for the common tool signature and not used.

Configuration comes from the environment and from nowhere else (docs/CONVENTIONS.md, Secrets):
`SENTRY_AUTH_TOKEN` is required, `SENTRY_ORG` and `SENTRY_PROJECT` name the organisation and the
default project, and `SENTRY_BASE_URL` (default `https://sentry.io`) points at a self-hosted
server. The token is sent only in the `Authorization` header and never appears in a result, an
error message or a log line. Neither tool is ever handed to a sandboxed agent (`SANDBOX_DENIED`
in `tools.registry`), because the sandbox is offline.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from factory.exit import AuthOrConfigError
from tools._common import (
    DEFAULT_MAX_BYTES,
    TIMEOUT_S,
    ToolError,
    ToolExecutionError,
    ToolInputError,
    ToolNotFoundError,
    truncate,
)

DEFAULT_BASE_URL = "https://sentry.io"
"""Sentry server used when `SENTRY_BASE_URL` is unset."""

MAX_FRAMES = 30
"""Most stack frames kept per exception, the innermost ones."""

MAX_BREADCRUMBS = 30
"""Most breadcrumbs kept, the ones nearest the failure."""

MAX_EVENTS = 50
"""Most events `list_events` returns."""

_MAX_TEXT = 500


class ToolConfigError(ToolError, AuthOrConfigError):
    """The Sentry token, organisation or project is missing, or Sentry rejected the token (exit 4)."""


def _env(name: str) -> str | None:
    """Return the environment variable `name`, or None when it is unset or blank."""
    value = os.environ.get(name, "").strip()
    return value or None


def _token() -> str:
    """Return `SENTRY_AUTH_TOKEN`, or raise `ToolConfigError` naming the variable, never its value."""
    token = _env("SENTRY_AUTH_TOKEN")
    if token is None:
        raise ToolConfigError(
            "SENTRY_AUTH_TOKEN is not set",
            hint="store it with `agb sys secrets set SENTRY_AUTH_TOKEN` and export it",
        )
    return token


def _setting(name: str, override: str | None = None) -> str:
    """Return `override` when given, else the environment variable `name`; raise when neither is set."""
    value = (override or "").strip() or _env(name)
    if value is None:
        raise ToolConfigError(f"{name} is not set", hint=f"export {name} or pass it as an argument")
    return value


def _segment(value: str, what: str) -> str:
    """Return `value` when it is safe as one URL path segment, else raise `ToolInputError`."""
    if not value or not all(c.isalnum() or c in "-_." for c in value) or value in {".", ".."}:
        raise ToolInputError(f"invalid {what}: {value!r}")
    return value


def _get(path: str, transport: httpx.BaseTransport | None) -> Any:
    """GET `path` from the configured Sentry server and return the decoded JSON body.

    Maps 401 and 403 to `ToolConfigError`, 404 to `ToolNotFoundError`, and any other failure, a
    network error or a non-JSON body to `ToolExecutionError`. Messages never carry the token.
    """
    token = _token()
    base = (_env("SENTRY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    try:
        with httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_S,
            transport=transport,
        ) as client:
            response = client.get(path)
    except httpx.HTTPError as exc:
        raise ToolExecutionError(f"could not reach Sentry: {type(exc).__name__}") from None
    if response.status_code in (401, 403):
        raise ToolConfigError(
            f"Sentry rejected the token (HTTP {response.status_code})",
            hint="check SENTRY_AUTH_TOKEN and its project:read and event:read scopes",
        )
    if response.status_code == 404:
        raise ToolNotFoundError("Sentry has no such organisation, project or issue")
    if response.status_code >= 400:
        raise ToolExecutionError(f"Sentry answered HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise ToolExecutionError("Sentry returned a body that is not JSON") from None


def _compact(document: Any, max_bytes: int) -> str:
    """Serialise `document` as compact JSON and cut it to `max_bytes` with the usual marker."""
    text = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    return truncate(text.encode("utf-8"), max_bytes)


def _short(value: Any) -> Any:
    """Return `value`, with a string cut to a bounded length."""
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return value[:_MAX_TEXT] + "..."
    return value


def _tag(tags: list[dict[str, Any]], key: str) -> str | None:
    """Return the value of the tag `key` in a Sentry tag list, or None when it is absent."""
    for tag in tags:
        if tag.get("key") == key:
            return tag.get("value")
    return None


def _parse_time(text: str, what: str) -> datetime:
    """Parse an ISO 8601 time, taking no zone as UTC; raise `ToolInputError` when malformed."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ToolInputError(
            f"invalid {what}: {text!r}", hint="use an ISO 8601 time such as 2026-11-01T00:00:00Z"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _exceptions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the exceptions of an `exception` entry with their innermost frames, normalised."""
    out = []
    for exc in entry.get("data", {}).get("values") or []:
        frames = (exc.get("stacktrace") or {}).get("frames") or []
        out.append(
            {
                "type": exc.get("type"),
                "value": _short(exc.get("value")),
                "frames": [
                    {
                        "file": f.get("filename") or f.get("absPath"),
                        "function": f.get("function"),
                        "line": f.get("lineNo"),
                        "in_app": f.get("inApp"),
                    }
                    for f in frames[-MAX_FRAMES:]
                ],
            }
        )
    return out


def _crumbs(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the last `MAX_BREADCRUMBS` breadcrumbs of a `breadcrumbs` entry, normalised."""
    values = entry.get("data", {}).get("values") or []
    return [
        {
            "time": c.get("timestamp"),
            "category": c.get("category"),
            "level": c.get("level"),
            "message": _short(c.get("message")),
        }
        for c in values[-MAX_BREADCRUMBS:]
    ]


def list_events(
    repo_path: str | Path,
    since: str,
    project: str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Return compact JSON listing a project's events created at or after `since`.

    `since` is an ISO 8601 time. `project` overrides `SENTRY_PROJECT`; the organisation is
    `SENTRY_ORG`. The result is `{"project", "events": [...]}` with at most `MAX_EVENTS` events,
    newest first, each with id, issue id, title, time, release and environment, cut to
    `max_bytes`. Raises `ToolConfigError` for a missing token, organisation or project or a
    rejected token, `ToolInputError` for a malformed argument, `ToolNotFoundError` for an unknown
    project and `ToolExecutionError` for any other failure. `transport` is for tests; the runtime
    never passes it. Reads no file under `repo_path`.
    """
    _token()
    cutoff = _parse_time(since, "since")
    org = _segment(_setting("SENTRY_ORG"), "organisation")
    slug = _segment(_setting("SENTRY_PROJECT", project), "project")
    body = _get(f"/api/0/projects/{org}/{slug}/events/", transport)
    if not isinstance(body, list):
        raise ToolExecutionError("Sentry returned an unexpected events payload")
    events: list[tuple[datetime, dict[str, Any]]] = []
    for e in body:
        created = e.get("dateCreated")
        if not created:
            continue
        when = _parse_time(created, "event time")
        if when < cutoff:
            continue
        tags = e.get("tags") or []
        entry = {
            "id": e.get("eventID") or e.get("id"),
            "issue_id": e.get("groupID"),
            "title": _short(e.get("title") or e.get("message")),
            "time": created,
            "release": _tag(tags, "release"),
            "environment": _tag(tags, "environment"),
        }
        events.append((when, entry))
    events.sort(key=lambda pair: pair[0], reverse=True)
    listed = [entry for _, entry in events[:MAX_EVENTS]]
    return _compact({"project": slug, "events": listed}, max_bytes)


def read_issue(
    repo_path: str | Path,
    id: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Return compact JSON for the Sentry issue `id` and its latest event, cut to `max_bytes`.

    The result holds the title, level, status, culprit, the event count and the affected-user
    count, the first and last seen times, the release tag of the latest event and the first and
    last release, the exceptions with their innermost `MAX_FRAMES` frames, and the last
    `MAX_BREADCRUMBS` breadcrumbs. `id` is the numeric issue id. Raises the same errors as
    `list_events`, with `ToolNotFoundError` for an unknown issue. `transport` is for tests; the
    runtime never passes it. Reads no file under `repo_path`.
    """
    _token()
    issue_id = _segment(str(id), "issue id")
    issue = _get(f"/api/0/issues/{issue_id}/", transport)
    event = _get(f"/api/0/issues/{issue_id}/events/latest/", transport)
    if not isinstance(issue, dict) or not isinstance(event, dict):
        raise ToolExecutionError("Sentry returned an unexpected issue payload")
    exceptions: list[dict[str, Any]] = []
    breadcrumbs: list[dict[str, Any]] = []
    for entry in event.get("entries") or []:
        if entry.get("type") == "exception":
            exceptions += _exceptions(entry)
        elif entry.get("type") == "breadcrumbs":
            breadcrumbs += _crumbs(entry)
    document = {
        "id": issue.get("id"),
        "short_id": issue.get("shortId"),
        "title": _short(issue.get("title")),
        "culprit": _short(issue.get("culprit")),
        "level": issue.get("level"),
        "status": issue.get("status"),
        "count": int(issue.get("count") or 0),
        "user_count": issue.get("userCount"),
        "first_seen": issue.get("firstSeen"),
        "last_seen": issue.get("lastSeen"),
        "release": _tag(event.get("tags") or [], "release"),
        "first_release": (issue.get("firstRelease") or {}).get("version"),
        "last_release": (issue.get("lastRelease") or {}).get("version"),
        "event_id": event.get("eventID") or event.get("id"),
        "exceptions": exceptions,
        "breadcrumbs": breadcrumbs,
    }
    return _compact(document, max_bytes)
