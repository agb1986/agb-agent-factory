"""Build the fixture repositories under `fixtures/repos/`.

Each fixture is a source directory holding a `base/` tree, a `head/` tree and a `defects.yaml`, and
for a decomposition fixture a `ground-truth.yaml`.
`build` turns it into a git repository with two commits on two branches: `base` and, on top of it,
`head`. Author, committer, dates and branch names are fixed and no hooks or user git config apply,
so the same sources always produce the same commit SHAs.

Run `uv run python -m fixtures.build` to rebuild every fixture, name some to build only those, or
pass `--list` to see what exists. To add a fixture, put its directory under `fixtures/src/` (or
`fixtures/handmade/`) and run the build; `discover` finds it by directory name. `ROOTS` is the
list of source roots later issues extend.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from factory.exit import (
    AuthOrConfigError,
    ExitCode,
    FactoryError,
    NotFoundError,
    error_envelope,
    exit_code,
)
from fixtures.ground_truth import GroundTruth, check_ground_truth, load_ground_truth
from fixtures.schema import DefectManifest, check_against_tree, load_manifest

FIXTURES_DIR = Path(__file__).resolve().parent
SRC_DIR = FIXTURES_DIR / "src"
HANDMADE_DIR = FIXTURES_DIR / "handmade"
REPOS_DIR = FIXTURES_DIR / "repos"

# Directories whose children are fixtures. A later issue adds a root here, or just a directory.
ROOTS: tuple[Path, ...] = (SRC_DIR, HANDMADE_DIR)

BASE_REF = "base"
HEAD_REF = "head"
AUTHOR_NAME = "Fixture Author"
AUTHOR_EMAIL = "fixtures@example.invalid"
BASE_DATE = "2026-01-05T09:00:00+0000"
HEAD_DATE = "2026-01-05T10:00:00+0000"

_SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
_SKIP_SUFFIXES = (".pyc", ".pyo")


class BuildError(FactoryError):
    """A fixture could not be built: bad source tree, or a git command failed."""


class FixtureNotFound(NotFoundError):
    """A requested fixture name matches no source directory."""


@dataclass(frozen=True)
class Fixture:
    """A source directory that `build` can turn into a repository."""

    name: str
    source: Path

    @property
    def manifest_path(self) -> Path:
        """The fixture's `defects.yaml`."""
        return self.source / "defects.yaml"

    def load_manifest(self) -> DefectManifest:
        """Load and validate `defects.yaml`; its `fixture` field must equal the directory name."""
        return load_manifest(self.manifest_path, expected_fixture=self.name)

    @property
    def ground_truth_path(self) -> Path:
        """The fixture's `ground-truth.yaml`; only a decomposition fixture has one."""
        return self.source / "ground-truth.yaml"

    def load_ground_truth(self) -> GroundTruth | None:
        """Load and validate `ground-truth.yaml`, or return None when the fixture has none."""
        if not self.ground_truth_path.exists():
            return None
        return load_ground_truth(self.ground_truth_path, expected_fixture=self.name)


@dataclass(frozen=True)
class BuiltFixture:
    """A built repository: where it is and the commits on its `base` and `head` branches."""

    name: str
    path: Path
    base_sha: str
    head_sha: str


def discover(roots: Iterable[Path] | None = None) -> dict[str, Fixture]:
    """Return every fixture under `roots` (default `ROOTS`), keyed by directory name, sorted.

    A fixture is a directory with a `base/` and a `head/` child. Two fixtures with one name are a
    `BuildError`, since the name is the handle an eval case uses.
    """
    found: dict[str, Fixture] = {}
    for root in ROOTS if roots is None else roots:
        if not root.is_dir():
            continue
        for source in sorted(root.iterdir()):
            if not (source / BASE_REF).is_dir() or not (source / HEAD_REF).is_dir():
                continue
            if source.name in found:
                raise BuildError(
                    f"fixture name {source.name!r} is used twice: {found[source.name].source} "
                    f"and {source}"
                )
            found[source.name] = Fixture(source.name, source)
    return dict(sorted(found.items()))


def build(
    names: Sequence[str] | None = None,
    *,
    dest: Path = REPOS_DIR,
    roots: Iterable[Path] | None = None,
) -> list[BuiltFixture]:
    """Build the named fixtures (all when `names` is None or empty) under `dest`.

    Each fixture lands in `dest/<name>/`, which is deleted first, so a rebuild never mixes old and
    new files. Unknown names raise `FixtureNotFound` before anything is touched. The manifest, and
    the ground truth of a fixture that has one, are checked against the head tree first; a stale
    `defects.yaml` or `ground-truth.yaml` raises `FixtureManifestError`.
    """
    available = discover(roots)
    wanted = list(names) if names else list(available)
    missing = [name for name in wanted if name not in available]
    if missing:
        raise FixtureNotFound(
            f"no fixture named {', '.join(repr(m) for m in missing)}",
            hint=f"known fixtures: {', '.join(available) or 'none'}",
        )
    dest.mkdir(parents=True, exist_ok=True)
    return [_build_one(available[name], dest / name) for name in wanted]


def _build_one(fixture: Fixture, repo: Path) -> BuiltFixture:
    """Build one fixture into `repo`, replacing whatever is there."""
    head_tree = fixture.source / HEAD_REF
    manifest = fixture.load_manifest()
    check_against_tree(manifest, lambda rel: _read_text(head_tree / rel))
    truth = fixture.load_ground_truth()
    if truth is not None:
        files = [p.relative_to(head_tree).as_posix() for p in _tree_paths(head_tree)]
        check_ground_truth(truth, manifest, files, lambda rel: _read_text(head_tree / rel))

    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "--template=", f"--initial-branch={BASE_REF}")

    _copy_tree(fixture.source / BASE_REF, repo)
    _commit(repo, f"base: {fixture.name}", BASE_DATE)
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-q", "-b", HEAD_REF)
    _clear_worktree(repo)
    _copy_tree(head_tree, repo)
    _commit(repo, f"head: {fixture.name}", HEAD_DATE)
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    if _git(repo, "diff", "--name-only", BASE_REF, HEAD_REF).strip() == "":
        raise BuildError(f"fixture {fixture.name!r} has no difference between base and head")
    return BuiltFixture(fixture.name, repo, base_sha, head_sha)


def _read_text(path: Path) -> str | None:
    """Return the text of `path`, or None when it is not a readable file."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None


def _tree_paths(src: Path) -> list[Path]:
    """Return everything under `src`, directories included, in sorted order and without caches."""
    return [
        path
        for path in sorted(src.rglob("*"))
        if not any(part in _SKIP_DIRS for part in path.relative_to(src).parts)
        and path.suffix not in _SKIP_SUFFIXES
    ]


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy `src` into `dest` in sorted order, skipping caches. File modes come from `src`."""
    for path in _tree_paths(src):
        target = dest / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o755 if os.access(path, os.X_OK) else 0o644)


def _clear_worktree(repo: Path) -> None:
    """Delete everything in `repo` except `.git`, so the next tree is an exact replacement."""
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _commit(repo: Path, message: str, date: str) -> None:
    """Stage everything and commit it with the fixed identity and `date`; no hooks run."""
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "commit.gpgsign=false",
        "-c", "core.hooksPath=" + os.devnull,
        "commit", "-q", "--no-verify", "--allow-empty-message", "-m", message,
        date=date,
    )  # fmt: skip


def _git(repo: Path, *args: str, date: str = BASE_DATE) -> str:
    """Run git in `repo` under a fixed, minimal environment and return its stdout.

    No user or system config is read and the locale is C, so the result never depends on the
    machine. A missing `git` is an `AuthOrConfigError`; any other failure is a `BuildError`.
    """
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.devnull,
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_DATE": date,
    }
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.fileMode=true", *args]
    try:
        done = subprocess.run(
            cmd, cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", check=False
        )
    except FileNotFoundError as exc:
        raise AuthOrConfigError("git is not installed or not on PATH") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise BuildError(
            f"git {' '.join(args[:3])} failed in {repo}: {detail[-1] if detail else done.returncode}"
        )
    return done.stdout


def _fail(exc: Exception, *, as_json: bool) -> int:
    """Report `exc` on stderr and return its exit code. Stdout stays untouched."""
    if as_json:
        print(json.dumps(error_envelope(exc)), file=sys.stderr)
    else:
        print(getattr(exc, "message", None) or str(exc) or type(exc).__name__, file=sys.stderr)
        hint = getattr(exc, "hint", None)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
    return exit_code(exc)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for `python -m fixtures.build`."""
    parser = argparse.ArgumentParser(
        prog="python -m fixtures.build",
        description="Build the fixture repositories under fixtures/repos/.",
        epilog="example: uv run python -m fixtures.build off-by-one mixed --json",
    )
    parser.add_argument("names", nargs="*", help="fixtures to build (default: all)")
    parser.add_argument("--list", action="store_true", help="list fixture names and build nothing")
    parser.add_argument(
        "--json", action="store_true", help="print one JSON document instead of text"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the build command and return its exit code.

    Prints `name<TAB>base<TAB>head<TAB>path` per fixture, or `{"fixtures": [...]}` under `--json`.
    Failures go to stderr, as the JSON error envelope under `--json`, with the matching exit code.
    """
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else ExitCode.USAGE
    try:
        if args.list:
            names = list(discover())
            if args.json:
                print(json.dumps({"fixtures": [{"name": name} for name in names]}))
            else:
                print("\n".join(names))
            return ExitCode.OK
        built = build(args.names)
        if args.json:
            rows = [
                {"name": b.name, "path": str(b.path), "base": b.base_sha, "head": b.head_sha}
                for b in built
            ]
            print(json.dumps({"fixtures": rows}))
        else:
            for b in built:
                print(f"{b.name}\t{b.base_sha}\t{b.head_sha}\t{b.path}")
        return ExitCode.OK
    except Exception as exc:  # noqa: BLE001 - map every failure to its exit code
        return _fail(exc, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
