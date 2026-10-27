"""Fetch a labelled diff from GitHub by its commit pair.

The labels store only a repository and two SHAs (`evals/calibration/labels.py`), so calibration
fetches each diff again through `gh api .../compare/{base}...{head}` with the diff media type. `gh`
supplies the token, which reaches private repositories without the factory handling it.

A fetched diff is untrusted data: it is handed to the triage agent as text and never run, and
nothing here prints it. An error names the diff and quotes only the last line `gh` wrote to
stderr, which is its one-line summary of the HTTP failure.
"""

import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from evals.calibration.labels import LabelledDiff
from factory.exit import AuthOrConfigError, FactoryError

DIFF_MEDIA_TYPE = "application/vnd.github.diff"
FETCH_TIMEOUT_S = 120.0
"""How long one `gh api` call may take; GitHub gives up on a large compare well before this."""

_AUTH_MARKERS = ("HTTP 401", "HTTP 403", "gh auth login", "authentication")
"""`gh` stderr that means the token is missing, invalid, or cannot read the repository."""

type Run = Callable[..., subprocess.CompletedProcess[Any]]


class FetchError(FactoryError):
    """One or more diffs could not be fetched (exit 1); the message names each one."""


def fetch_diff(owner_repo: str, base: str, head: str, *, run: Run = subprocess.run) -> str:
    """Return the unified diff from `base` to `head` in `owner_repo`, as GitHub renders it.

    Raises `AuthOrConfigError` when `gh` is not installed or is not logged in with access to the
    repository, and `FetchError` for any other failure.
    """
    command = [
        "gh",
        "api",
        f"repos/{owner_repo}/compare/{base}...{head}",
        "-H",
        f"Accept: {DIFF_MEDIA_TYPE}",
    ]
    try:
        done = run(command, capture_output=True, check=False, timeout=FETCH_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise AuthOrConfigError(
            "gh is not installed", hint="install the GitHub CLI and run `gh auth login`"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"{owner_repo}: gh api timed out after {FETCH_TIMEOUT_S:.0f}s") from exc
    stderr = _text(done.stderr)
    last = stderr.strip().splitlines()[-1] if stderr.strip() else f"gh exited {done.returncode}"
    if done.returncode == 4 or any(marker in stderr for marker in _AUTH_MARKERS):
        raise AuthOrConfigError(
            f"{owner_repo}: gh cannot read the repository: {last}",
            hint="run `gh auth status` and log in with access to the repository",
        )
    if done.returncode != 0:
        raise FetchError(f"{owner_repo}: {last}")
    return _text(done.stdout)


def fetch_all(
    diffs: Sequence[LabelledDiff],
    *,
    fetch: Callable[[str, str, str], str] = fetch_diff,
) -> dict[str, str]:
    """Fetch every labelled diff and return the texts by label id, in label order.

    Every diff is tried before anything is raised, so one error names all the failures: an
    `AuthOrConfigError` when any of them was an auth failure, else a `FetchError`.
    """
    texts: dict[str, str] = {}
    failures: list[str] = []
    auth = False
    for diff in diffs:
        try:
            texts[diff.id] = fetch(diff.owner_repo, diff.base, diff.head)
        except AuthOrConfigError as exc:
            auth = True
            failures.append(f"{diff.id} ({exc.message})")
        except FetchError as exc:
            failures.append(f"{diff.id} ({exc.message})")
    if failures:
        message = f"could not fetch {len(failures)} of {len(diffs)} diffs: {'; '.join(failures)}"
        if auth:
            raise AuthOrConfigError(message, hint="run `gh auth status`")
        raise FetchError(
            message,
            hint="check that each commit pair still exists, and that gh can see private repositories",
        )
    return texts


def _text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")
