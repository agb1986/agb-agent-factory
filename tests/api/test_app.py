"""The HTTP trigger for `factory review`: argument mapping, status codes, report shape."""

import json
import sys
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.app as app_module
from api.app import ReviewRequest, app
from factory.exit import ExitCode

REPORT = {"ship": True, "score": 0.9}
ENVELOPE = {"error": {"code": 4, "message": "ANTHROPIC_API_KEY is not set", "hint": None}}
BODY = {"repo_path": "/repos/app", "base": "main", "head": "feature"}


def fake_main(
    code: int, *, out: str = "", err: str = "", seen: list[list[str]] | None = None
) -> Callable[[list[str]], int]:
    """Return a stand-in for `factory.cli.main` that prints `out` and `err` and returns `code`."""

    def main(argv: list[str]) -> int:
        if seen is not None:
            seen.append(argv)
        print(out, end="")
        print(err, end="", file=sys.stderr)
        return code

    return main


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_review_passes_every_argument_and_returns_the_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(app_module, "factory_main", fake_main(0, out=json.dumps(REPORT), seen=seen))
    body: dict[str, Any] = {
        **BODY,
        "reviewers": ["code-reviewer", "security-reviewer"],
        "allow_unpublished": True,
        "no_triage": True,
        "budget_usd": 0.5,
    }
    response = client.post("/review", json=body)
    assert response.status_code == 200
    assert response.json() == {"exit_code": 0, "report": REPORT}
    assert seen == [
        [
            "review",
            "/repos/app",
            "--base=main",
            "--head=feature",
            "--json",
            "--reviewers=code-reviewer,security-reviewer",
            "--allow-unpublished",
            "--no-triage",
            "--budget-usd=0.5",
        ]
    ]


def test_minimal_request_sends_only_the_required_arguments() -> None:
    assert ReviewRequest(**BODY).argv() == [
        "review",
        "/repos/app",
        "--base=main",
        "--head=feature",
        "--json",
    ]


def test_a_change_that_does_not_ship_is_still_a_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "factory_main", fake_main(1, out=json.dumps({"ship": False})))
    response = client.post("/review", json=BODY)
    assert response.status_code == 200
    assert response.json() == {"exit_code": 1, "report": {"ship": False}}


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ExitCode.ERROR, 500),
        (ExitCode.USAGE, 422),
        (ExitCode.NOT_FOUND, 404),
        (ExitCode.AUTH_OR_CONFIG, 503),
    ],
)
def test_a_run_without_a_report_returns_the_envelope_with_its_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, code: ExitCode, status: int
) -> None:
    # The report prose comes first on stderr; the envelope is the last line.
    monkeypatch.setattr(
        app_module, "factory_main", fake_main(code, err="noise\n" + json.dumps(ENVELOPE) + "\n")
    )
    response = client.post("/review", json=BODY)
    assert response.status_code == status
    assert response.json() == ENVELOPE


def test_a_run_with_neither_report_nor_envelope_is_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "factory_main", fake_main(1, err="boom\n"))
    assert client.post("/review", json=BODY).status_code == 500


@pytest.mark.parametrize("field", ["repo_path", "base", "head"])
@pytest.mark.parametrize("value", ["--help", "-x", ""])
def test_a_value_that_reads_as_a_flag_is_rejected(
    client: TestClient, field: str, value: str
) -> None:
    response = client.post("/review", json={**BODY, field: value})
    assert response.status_code == 422


def test_a_reviewer_that_reads_as_a_flag_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewRequest(**BODY, reviewers=["--json"])


def test_the_budget_must_be_positive(client: TestClient) -> None:
    assert client.post("/review", json={**BODY, "budget_usd": 0}).status_code == 422


def test_the_real_cli_reports_a_missing_key_as_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """End to end through `factory.cli.main`, with no key set and no network."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("FACTORY_TRACE_DB", str(tmp_path / "traces.duckdb"))
    response = client.post("/review", json={**BODY, "allow_unpublished": True, "no_triage": True})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["error"]["message"]
