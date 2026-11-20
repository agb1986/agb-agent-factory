"""The doc drift check: do the docs and docstrings of a work tree still describe its code?

A documentation agent is graded on the work tree it left behind, so the `sandbox_tests` grader can
require that chosen source files and docs agree (`ExpectedRun.doc_drift`). `check_doc_drift` reads
source with `ast` and Markdown as text, runs nothing, and reports four kinds of drift:

- **missing docstring**: a public function or class in one of `paths`, or a public method of a
  public class there, has no docstring in the work tree. Public means the name does not start with
  an underscore; dunder methods such as `__init__` are not counted.
- **undocumented name**: a public top-level function, class or assignment in one of `paths` is not
  named in any of the docs files. A name counts as named when it is an identifier inside a
  backticked span (`` `load` ``, `` `load()` ``, `` `config.load(path)` ``) in one of them.
- **stale name**: a backticked span in a docs file that is a bare identifier, optionally followed
  by `()`, names something that was a public top-level name of one of `paths` at the fixture's
  `base` or `HEAD` and is not one in the work tree any more.
- **code changed**: a file that differs from HEAD changed in a way a docs change may not, by the
  rules of `pipelines.docs_check.check_change`: the same rules the build loop applies.

The docs files are the work tree's files that match one of `docs`, as `PurePosixPath.full_match`
globs, so a case can name a file the agent is expected to create. What the check does not cover:
prose that is wrong about what the code does, a parameter or return value the docs leave out, a
docs link that does not resolve, and any language but Python. It is a departure from the
`doc-drift-detector` skill, which scores staleness from history and checks links: this check is
deterministic and in-repo, like `evals.public_api`, and depends on no skill.
"""

import ast
import re
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

from fixtures.build import BASE_REF
from pipelines.docs_check import DocsCheckError, check_change
from tools._common import ToolError, git, resolve_inside, resolve_root

_SPAN = re.compile(r"`([^`\n]+)`")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BARE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?")
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def missing_docstrings(source: str) -> list[str]:
    """Return each public function, class and public method of a public class with no docstring.

    Methods are named `Class.method`. Raises `SyntaxError` when `source` does not parse.
    """
    missing: list[str] = []
    for node in ast.parse(source).body:
        if not isinstance(node, _DEFS) or node.name.startswith("_"):
            continue
        if ast.get_docstring(node) is None:
            missing.append(node.name)
        if isinstance(node, ast.ClassDef):
            missing.extend(
                f"{node.name}.{member.name}"
                for member in node.body
                if isinstance(member, _DEFS)
                and not member.name.startswith("_")
                and ast.get_docstring(member) is None
            )
    return missing


def top_level_names(source: str) -> set[str]:
    """Return the public functions, classes and assigned names at the top of the module `source`.

    Raises `SyntaxError` when `source` does not parse.
    """
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, _DEFS):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {name for name in names if not name.startswith("_")}


def named_in(text: str) -> set[str]:
    """Return every identifier inside a backticked span of the Markdown `text`."""
    return {ident for span in _SPAN.findall(text) for ident in _IDENT.findall(span)}


def bare_references(text: str) -> set[str]:
    """Return the backticked spans of `text` that are one identifier, with or without `()`."""
    found: set[str] = set()
    for span in _SPAN.findall(text):
        if match := _BARE.fullmatch(span.strip()):
            found.add(match.group(1))
    return found


def check_doc_drift(
    root: str | Path, paths: Sequence[str], docs: Sequence[str], changed: Sequence[str]
) -> list[str]:
    """Return the drift between the source `paths` and the `docs` globs in the work tree `root`.

    `changed` lists the paths that differ from HEAD (`evals.graders.sandbox_tests.changed_files`),
    which the code-changed rule reads. Empty when there is no drift. A path that is missing,
    outside the repository or does not parse is reported, not raised.
    """
    where = resolve_root(root)
    failures: list[str] = []
    doc_text = "\n".join(_read(where / path) for path in _docs_files(where, docs))
    named = named_in(doc_text)
    referenced = bare_references(doc_text)
    current: set[str] = set()
    earlier: set[str] = set()
    for path in paths:
        try:
            rel = resolve_inside(where, path).as_posix()
        except ToolError as exc:
            failures.append(f"doc_drift: {path}: {exc.message}")
            continue
        target = where / rel
        if not target.is_file():
            failures.append(f"doc_drift: {rel} is not a file in the work tree")
            continue
        try:
            source = _read(target)
            names = top_level_names(source)
            undocumented = missing_docstrings(source)
        except SyntaxError as exc:
            failures.append(f"doc_drift: {rel} does not parse (line {exc.lineno})")
            continue
        current |= names
        earlier |= _names_at(where, BASE_REF, rel) | _names_at(where, "HEAD", rel)
        if undocumented:
            failures.append(f"doc_drift: {rel} has no docstring for {', '.join(undocumented)}")
        if unnamed := sorted(names - named):
            failures.append(f"doc_drift: no docs file names {', '.join(unnamed)} from {rel}")
    if stale := sorted((referenced & earlier) - current):
        failures.append(f"doc_drift: the docs still name {', '.join(stale)}, gone from the code")
    failures.extend(_code_changes(where, changed))
    return failures


def _code_changes(root: Path, changed: Sequence[str]) -> list[str]:
    failures: list[str] = []
    for path in changed:
        before = _show(root, "HEAD", path)
        target = root / PurePosixPath(path)
        after = _read(target) if target.is_file() else None
        if before is None and after is None:
            continue
        action = "created" if before is None else "deleted" if after is None else "modified"
        try:
            check_change(path, action, before, after)
        except DocsCheckError as exc:
            failures.append(f"doc_drift: {exc.message}")
    return failures


def _docs_files(root: Path, globs: Sequence[str]) -> Iterator[str]:
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".git" in rel.parts or not path.is_file():
            continue
        pure = PurePosixPath(rel.as_posix())
        if any(pure.full_match(glob) for glob in globs):
            yield pure.as_posix()


def _names_at(root: Path, ref: str, path: str) -> set[str]:
    source = _show(root, ref, path)
    if source is None:
        return set()
    try:
        return top_level_names(source)
    except SyntaxError:
        return set()


def _show(root: Path, ref: str, path: str) -> str | None:
    proc = git(["show", f"{ref}:{path}"], root)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


__all__ = [
    "bare_references",
    "check_doc_drift",
    "missing_docstrings",
    "named_in",
    "top_level_names",
]
