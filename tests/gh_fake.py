"""A fake `gh` on PATH and a repository whose `origin` is on github.com, for the GitHub tool tests.

The fake answers `gh api` from a route table keyed by `"METHOD path"` and logs every call, so a
test can assert what would have been sent. An unknown route answers like GitHub's 404. Any other
`gh` command, such as `gh auth git-credential`, fails. The real `gh` is never run, and nothing
reaches github.com: the repository's local config rewrites pushes to its origin URL to a bare
repository under `tmp_path`.
"""

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

TOKEN = "ghp_FAKE_TOKEN_THAT_MUST_NEVER_BE_SHOWN"
ORIGIN = "https://github.com/acme/widgets.git"

_SCRIPT = """\
#!{python}
import json, os, sys

args = sys.argv[1:]
stdin = sys.stdin.read() if "--input" in args else None
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"args": args, "stdin": stdin}}) + "\\n")
if args[:1] != ["api"]:
    sys.stderr.write("fake gh: only gh api is faked\\n")
    sys.exit(1)
method = args[args.index("-X") + 1]
with open(os.environ["FAKE_GH_ROUTES"], encoding="utf-8") as f:
    route = json.load(f).get(method + " " + args[-1])
if route is None:
    sys.stderr.write("gh: Not Found (HTTP 404)\\n")
    sys.exit(1)
if "status" in route:
    sys.stderr.write("gh: failed (HTTP " + str(route["status"]) + ")\\n")
    sys.exit(1)
if "fail" in route:
    sys.stderr.write(route["fail"] + "\\n")
    sys.exit(1)
if "raw" in route:
    sys.stdout.write(route["raw"])
    sys.exit(0)
body = route.get("json")
if body is not None:
    sys.stdout.write(json.dumps([body] if "--slurp" in args else body))
"""


@dataclass(frozen=True)
class Call:
    """One logged `gh api` call."""

    method: str
    path: str
    paginate: bool
    data: Any


class FakeGh:
    """The fake `gh`: set routes with `route`, read what was sent with `calls`."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir()
        script = bin_dir / "gh"
        script.write_text(_SCRIPT.format(python=sys.executable), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self._routes_path = tmp_path / "gh-routes.json"
        self._log = tmp_path / "gh-log.jsonl"
        self._routes: dict[str, Any] = {}
        self._save()
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("FAKE_GH_ROUTES", str(self._routes_path))
        monkeypatch.setenv("FAKE_GH_LOG", str(self._log))
        monkeypatch.setenv("GH_TOKEN", TOKEN)

    def _save(self) -> None:
        self._routes_path.write_text(json.dumps(self._routes), encoding="utf-8")

    def route(self, method: str, path: str, json_body: Any = None, **outcome: Any) -> None:
        """Answer `method path` with `json_body`, or with `status=`, `fail=` or `raw=`."""
        self._routes[f"{method} {path}"] = outcome or {"json": json_body}
        self._save()

    def raw_log(self) -> str:
        """Return the log as written, to check nothing secret was passed."""
        return self._log.read_text(encoding="utf-8") if self._log.exists() else ""

    def calls(self) -> list[Call]:
        """Return every `gh api` call made so far, in order."""
        found = []
        for line in self.raw_log().splitlines():
            entry = json.loads(line)
            args = entry["args"]
            method = args[args.index("-X") + 1] if "-X" in args else ""
            data = json.loads(entry["stdin"]) if entry["stdin"] else None
            found.append(Call(method, args[-1], "--paginate" in args, data))
        return found

    def writes(self) -> list[Call]:
        """Return the calls that are not GETs."""
        return [c for c in self.calls() if c.method != "GET"]


def git(cwd: Path, *args: str) -> str:
    """Run `git` with a fixed identity and no signing, returning stdout."""
    proc = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return proc.stdout.strip()


@dataclass(frozen=True)
class GithubRepo:
    """A work tree whose `origin` is `ORIGIN`, the bare repository pushes land in, and `HEAD`."""

    root: Path
    bare: Path
    head: str


def github_repo(tmp_path: Path, origin: str | None = ORIGIN) -> GithubRepo:
    """Build a one-commit work tree with `origin` set, and send its pushes to a local bare repo."""
    bare = tmp_path / "pushed.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    root = tmp_path / "widgets"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("print('hello')\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    if origin is not None:
        git(root, "remote", "add", "origin", origin)
    git(root, "config", f"url.{bare}.pushInsteadOf", ORIGIN)
    return GithubRepo(root, bare, git(root, "rev-parse", "HEAD"))
