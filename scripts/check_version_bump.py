"""Fail when an agent's prompt, schemas or questions changed without a version bump.

Usage: `python scripts/check_version_bump.py [base]`, run from inside the repository. `base` is a
git ref and defaults to `origin/main`. The working tree is compared with the merge base of `base`
and `HEAD`, so uncommitted edits count, which is what the pre-commit hook needs, and so does a
branch checked out in CI.

For every agent directory (one holding an `agent.yaml`, either `agents/<name>/` or
`agents/<name>/<version>/`) the script compares three things against the merge base: the system
prompt file, the schemas file, and the manifest's `questions` block. If any differs while the
manifest's `version` is the same, the agent is listed and the script exits 1. An agent directory
that does not exist at the merge base is new and needs no bump.

Exit codes follow `factory`: 0 clean, 1 a missing bump, 2 the script could not run (no git, an
unknown base, an unreadable manifest). It never guesses a base. The rules are in
`docs/CONVENTIONS.md` under "Agent address".
"""

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml

DEFAULT_BASE = "origin/main"
AGENTS = "agents"
MANIFEST = "agent.yaml"
DEFAULT_PROMPT = "system.md"
DEFAULT_SCHEMAS = "schemas.py"


type Reader = Callable[[str], str | None]


class ScriptError(Exception):
    """The check could not run; the message says why (exit 2)."""


class Violation(NamedTuple):
    """An agent directory whose guarded content changed while its version stayed `version`."""

    directory: str
    version: str
    changed: tuple[str, ...]


class _Signature(NamedTuple):
    version: str
    prompt: str | None
    schemas: str | None
    questions: Any


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True, encoding="utf-8"
        )
    except OSError as exc:
        raise ScriptError(f"cannot run git: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise ScriptError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return done.stdout


def _parse(text: str, where: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScriptError(f"{where}: malformed YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ScriptError(f"{where}: manifest must be a mapping of fields")
    return data


def _signature(data: dict[str, Any], where: str, read: Reader) -> _Signature:
    version = data.get("version")
    if not isinstance(version, str):
        raise ScriptError(f"{where}: version: {version!r} is not a string")
    directory = str(Path(where).parent)
    prompt_name = str(data.get("system_prompt", DEFAULT_PROMPT))
    schemas_name = str(data.get("schemas", DEFAULT_SCHEMAS))
    return _Signature(
        version,
        read(f"{directory}/{prompt_name}"),
        read(f"{directory}/{schemas_name}"),
        data.get("questions"),
    )


def _tree_reader(repo: Path, commit: str) -> Reader:
    def read(path: str) -> str | None:
        try:
            return _git(repo, "show", f"{commit}:{path}")
        except ScriptError:
            return None

    return read


def _worktree_reader(repo: Path) -> Reader:
    def read(path: str) -> str | None:
        file = repo / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    return read


def _base_manifests(repo: Path, commit: str) -> list[str]:
    listing = _git(repo, "ls-tree", "-r", "--name-only", commit, "--", AGENTS)
    return [line for line in listing.splitlines() if line.rsplit("/", 1)[-1] == MANIFEST]


def _tree_manifests(repo: Path) -> list[str]:
    root = repo / AGENTS
    if not root.is_dir():
        return []
    found = [*root.glob(f"*/{MANIFEST}"), *root.glob(f"*/*/{MANIFEST}")]
    return sorted(path.relative_to(repo).as_posix() for path in found)


def check(repo: Path, base: str = DEFAULT_BASE) -> list[Violation]:
    """Return the agents under `repo` whose guarded content changed without a version change.

    Compares the working tree with the merge base of `base` and `HEAD`. Raises `ScriptError` when
    git fails, `base` does not resolve, or a manifest cannot be parsed.
    """
    merge_base = _git(repo, "merge-base", base, "HEAD").strip()
    old_read, new_read = _tree_reader(repo, merge_base), _worktree_reader(repo)
    old_manifests = set(_base_manifests(repo, merge_base))
    violations: list[Violation] = []
    for manifest in _tree_manifests(repo):
        if manifest not in old_manifests:
            continue
        old_text, new_text = old_read(manifest), new_read(manifest)
        if old_text is None or new_text is None:
            raise ScriptError(f"{manifest}: cannot read the manifest")
        old_data = _parse(old_text, f"{manifest} at {merge_base[:12]}")
        new_data = _parse(new_text, manifest)
        old = _signature(old_data, manifest, old_read)
        new = _signature(new_data, manifest, new_read)
        if old.version != new.version:
            continue
        changed = tuple(
            label
            for label, differs in (
                ("system prompt", old.prompt != new.prompt),
                ("schemas", old.schemas != new.schemas),
                ("questions", old.questions != new.questions),
            )
            if differs
        )
        if changed:
            violations.append(Violation(str(Path(manifest).parent), new.version, changed))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check from the repository containing the working directory; return the exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1 or args[:1] in (["-h"], ["--help"]):
        print(__doc__, file=sys.stderr)
        return 2
    base = args[0] if args else DEFAULT_BASE
    try:
        repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        violations = check(repo, base)
    except ScriptError as exc:
        print(f"check_version_bump: {exc}", file=sys.stderr)
        return 2
    for violation in violations:
        print(
            f"{violation.directory}: {', '.join(violation.changed)} changed since {base} "
            f"but version is still {violation.version}",
            file=sys.stderr,
        )
    if violations:
        print(
            "bump `version` in each agent.yaml listed above (see docs/CONVENTIONS.md)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
