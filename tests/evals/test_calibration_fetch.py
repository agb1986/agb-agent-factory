"""Tests for evals/calibration/fetch.py, with `gh` replaced by a fake: no network is used."""

import subprocess
from typing import Any

import pytest

from evals.calibration.fetch import DIFF_MEDIA_TYPE, FetchError, fetch_all, fetch_diff
from evals.calibration.labels import LabelledDiff
from factory.exit import AuthOrConfigError, ExitCode, exit_code

BASE = "a" * 40
HEAD = "b" * 40


class _Gh:
    """A fake `subprocess.run` for `gh` that records each command and returns a fixed result."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.result = (returncode, stdout, stderr)
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(command)
        assert kwargs["check"] is False and kwargs["timeout"] > 0
        return subprocess.CompletedProcess(command, *self.result)


def _gh(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> _Gh:
    return _Gh(returncode, stdout, stderr)


def _label(id: str) -> LabelledDiff:
    return LabelledDiff.model_validate(
        {
            "id": id,
            "repo": "https://github.com/acme/shop",
            "base": BASE,
            "head": HEAD if id.endswith("1") else "c" * 40,
            "private": True,
            "label": "safe",
            "severity": "none",
            "reason": "Docs only.",
            "source": "reader",
            "labelled_by": "tester",
            "labelled_on": "2026-10-27",
        }
    )


def test_a_diff_is_fetched_by_compare_with_the_diff_media_type() -> None:
    run = _gh(stdout=b"--- a/x.py\n+++ b/x.py\n")
    assert fetch_diff("acme/shop", BASE, HEAD, run=run) == "--- a/x.py\n+++ b/x.py\n"
    (command,) = run.calls
    assert command[:3] == ["gh", "api", f"repos/acme/shop/compare/{BASE}...{HEAD}"]
    assert f"Accept: {DIFF_MEDIA_TYPE}" in command


def test_bytes_that_are_not_utf8_are_replaced_not_raised() -> None:
    assert "�" in fetch_diff("acme/shop", BASE, HEAD, run=_gh(stdout=b"+caf\xe9\n"))


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (4, b"To get started with GitHub CLI, please run:  gh auth login\n"),
        (1, b"gh: Bad credentials (HTTP 401)\n"),
        (1, b"gh: Resource not accessible by integration (HTTP 403)\n"),
    ],
)
def test_an_auth_failure_is_an_auth_or_config_error(returncode: int, stderr: bytes) -> None:
    with pytest.raises(AuthOrConfigError) as info:
        fetch_diff("acme/shop", BASE, HEAD, run=_gh(returncode, stderr=stderr))
    assert exit_code(info.value) == ExitCode.AUTH_OR_CONFIG
    assert "acme/shop" in info.value.message


def test_another_failure_quotes_only_the_last_stderr_line() -> None:
    stderr = b"some noise\ngh: Not Found (HTTP 404)\n"
    with pytest.raises(FetchError) as info:
        fetch_diff("acme/shop", BASE, HEAD, run=_gh(1, stderr=stderr))
    assert info.value.message == "acme/shop: gh: Not Found (HTTP 404)"
    assert exit_code(info.value) == ExitCode.ERROR


def test_a_failure_with_no_stderr_names_the_exit_code() -> None:
    with pytest.raises(FetchError, match="gh exited 1"):
        fetch_diff("acme/shop", BASE, HEAD, run=_gh(1))


def test_gh_missing_is_an_auth_or_config_error() -> None:
    def run(command: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("gh")

    with pytest.raises(AuthOrConfigError, match="gh is not installed"):
        fetch_diff("acme/shop", BASE, HEAD, run=run)


def test_a_timeout_is_a_fetch_error() -> None:
    def run(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(FetchError, match="timed out"):
        fetch_diff("acme/shop", BASE, HEAD, run=run)


def test_fetch_all_returns_texts_by_id_in_label_order() -> None:
    texts = fetch_all([_label("shop#1"), _label("shop#2")], fetch=lambda repo, b, h: h[:3])
    assert texts == {"shop#1": "bbb", "shop#2": "ccc"}


def test_fetch_all_tries_every_diff_and_names_each_failure() -> None:
    tried: list[str] = []

    def fetch(repo: str, base: str, head: str) -> str:
        tried.append(head)
        raise FetchError(f"{repo}: gh: Not Found (HTTP 404)")

    with pytest.raises(FetchError) as info:
        fetch_all([_label("shop#1"), _label("shop#2")], fetch=fetch)
    assert len(tried) == 2
    assert "2 of 2" in info.value.message
    assert "shop#1" in info.value.message and "shop#2" in info.value.message


def test_fetch_all_reports_an_auth_failure_as_auth_or_config() -> None:
    def fetch(repo: str, base: str, head: str) -> str:
        if head == HEAD:
            raise AuthOrConfigError(f"{repo}: gh cannot read the repository")
        raise FetchError(f"{repo}: gh: Not Found (HTTP 404)")

    with pytest.raises(AuthOrConfigError, match="2 of 2"):
        fetch_all([_label("shop#1"), _label("shop#2")], fetch=fetch)
