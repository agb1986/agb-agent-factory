"""The docs check: a `docs` ticket may change prose, comments and docstrings, and no code.

The build loop runs it as the `docs` kind's `KindCheck` (`pipelines.behaviour.KindCheck`), around
`documentation-agent`. `before` records the commit the work starts from; `after_commit` reads each
path the round committed at that commit and at HEAD and refuses the round when a path changed in a
way a docs change may not:

- A docs path (`*.md` anywhere, or anything under `docs/`) may be created or modified.
- A deleted path is refused, docs included: removing a file is not documenting it.
- A Python file (`.py`) may only be modified, and its tree, parsed with `ast` with every docstring
  taken out, must be the same before and after. A docstring is a string constant that is the first
  statement of a module, class or function, and nothing else is: a string anywhere else is code. A
  file the round leaves unparsable is refused (`UnparsableSourceError`); a file that already did
  not parse at the start commit cannot be compared at all (`BaseUnparsableError`).
- A file in one of `COMMENT_SYNTAX`'s languages may only be modified, and only its comment lines:
  the lines left once each whole-line comment and blank line is dropped must be the same before
  and after. A trailing comment after code on the same line counts as code.
- Every other path is refused, so the check fails closed on a language it does not know.

A rename shows as a delete and a create (`pipelines.sandbox.changed_between`), so it is refused.
Test files and VCS and CI paths never reach the check: the agent's `files.deny` refuses them first.
Each refusal is its own `DocsCheckError`. The loop escalates the four a person can answer
(`CodeChangedError`, `UnparsableSourceError`, `SourceFileActionError`, `UncheckedFileError`) under
the `check_failed` trigger, and moves the item to `blocked` on `BaseUnparsableError`, a fault of
the repository at the ticket's `ref` that no answer to the docs round fixes (docs/CONVENTIONS.md,
Escalations).
"""

import ast
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from factory.exit import FactoryError
from registry.work_schemas import FileAction

if TYPE_CHECKING:  # pipelines.sandbox imports the grader, which imports evals.doc_drift and this
    from pipelines.sandbox import Sandbox

DOCS_GLOBS = ("**/*.md", "docs/**")
"""The paths a docs change may create or modify freely."""

COMMENT_SYNTAX: Mapping[str, str] = {
    ".sh": "#",
    ".bash": "#",
    ".toml": "#",
    ".yaml": "#",
    ".yml": "#",
    ".cfg": "#",
    ".ini": "#",
    ".js": "//",
    ".mjs": "//",
    ".ts": "//",
    ".go": "//",
    ".rs": "//",
    ".java": "//",
    ".c": "//",
    ".h": "//",
}
"""The file types whose comment lines may change, each with its whole-line comment marker.

Block comments (`/* */`) are not read as comments, so a change inside one is refused.
"""


class DocsCheckError(FactoryError):
    """A docs round changed something other than prose, comments and docstrings (exit 1)."""


class CodeChangedError(DocsCheckError):
    """A source file changed outside its comments and docstrings."""


class UnparsableSourceError(DocsCheckError):
    """The round left a Python file that does not parse, so its code cannot be compared."""


class BaseUnparsableError(DocsCheckError):
    """A Python file the round changed already did not parse at the commit the round started from.

    The fault is in the repository at the ticket's `ref`, not in the round, so the item goes to
    `blocked`, like a `refactor` run whose baseline suite fails.
    """


class SourceFileActionError(DocsCheckError):
    """The round created or deleted a source file, or deleted a docs file."""


class UncheckedFileError(DocsCheckError):
    """The round changed a file of a type the check cannot read for comments."""


def is_docs_path(path: str) -> bool:
    """Return whether `path` is prose a docs change may write: `*.md`, or under `docs/`."""
    pure = PurePosixPath(path)
    return any(pure.full_match(glob) for glob in DOCS_GLOBS)


def strip_docstrings(source: str) -> str:
    """Return the `ast.dump` of `source` with every docstring removed, for comparing code.

    Raises `SyntaxError` when `source` does not parse.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def code_lines(source: str, marker: str) -> list[str]:
    """Return the lines of `source` that are not blank and not a whole-line `marker` comment."""
    kept: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(marker):
            kept.append(line.rstrip())
    return kept


def check_change(path: str, action: FileAction, before: str | None, after: str | None) -> None:
    """Raise the `DocsCheckError` for one changed path, or return when a docs change may make it.

    `before` and `after` are the file's text at the start commit and at HEAD, None when absent.
    """
    if action == "deleted":
        raise SourceFileActionError(f"the docs round deleted {path}")
    if is_docs_path(path):
        return
    if action == "created":
        raise SourceFileActionError(f"the docs round created the source file {path}")
    suffix = PurePosixPath(path).suffix
    if suffix == ".py":
        try:
            base = strip_docstrings(before or "")
        except SyntaxError as exc:
            raise BaseUnparsableError(
                f"{path} does not parse at the round's start commit: {exc.msg}",
                hint="fix the file at the ticket's ref, then move the item back to ready",
            ) from exc
        try:
            head = strip_docstrings(after or "")
        except SyntaxError as exc:
            raise UnparsableSourceError(
                f"{path} does not parse after the round: {exc.msg}"
            ) from exc
        if base != head:
            raise CodeChangedError(f"{path} changed outside its docstrings and comments")
        return
    marker = COMMENT_SYNTAX.get(suffix)
    if marker is None:
        raise UncheckedFileError(
            f"{path} is not a docs file and its comments cannot be checked",
            hint=f"a docs change may edit *.md, docs/**, .py and {', '.join(COMMENT_SYNTAX)}",
        )
    if code_lines(before or "", marker) != code_lines(after or "", marker):
        raise CodeChangedError(f"{path} changed outside its comment lines")


@dataclass(slots=True)
class DocsCheck:
    """The `docs` kind's check: every committed path is docs, or source changed only in comments.

    It needs no sandbox run. `before` keeps the clone and the start commit; `after_commit` compares
    each path in `changed` between that commit and the clone's HEAD; `after_suite` does nothing.
    """

    clone: Path | None = None
    base: str | None = None

    def before(self, clone: Path, sandbox: Sandbox, head_sha: str) -> None:
        """Record the clone and the commit the docs change starts from."""
        self.clone = clone
        self.base = head_sha

    def after_commit(self, changed: Mapping[str, FileAction]) -> None:
        """Raise the first `DocsCheckError` among the paths this round committed."""
        if self.clone is None or self.base is None:
            raise DocsCheckError("the docs check ran after_commit before before")
        for path, action in sorted(changed.items()):
            check_change(
                path,
                action,
                _show(self.clone, self.base, path),
                _show(self.clone, "HEAD", path),
            )

    def after_suite(self, clone: Path, sandbox: Sandbox, entry: dict[str, Any]) -> None:
        """Do nothing: a docs change is checked on what it committed, not on the suite."""


def _show(clone: Path, ref: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=clone, capture_output=True, check=False
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


__all__ = [
    "COMMENT_SYNTAX",
    "DOCS_GLOBS",
    "BaseUnparsableError",
    "CodeChangedError",
    "DocsCheck",
    "DocsCheckError",
    "SourceFileActionError",
    "UncheckedFileError",
    "UnparsableSourceError",
    "check_change",
    "code_lines",
    "is_docs_path",
    "strip_docstrings",
]
