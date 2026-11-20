"""The public names and signatures of a Python module, compared between HEAD and the work tree.

A refactor must leave the callers of a module working, so the `sandbox_tests` grader can require
that the public API of chosen files did not change (`ExpectedRun.public_api`). `public_api` reads
one module's source with `ast` and returns a mapping from each public name to its signature. The
comparison is of source text, so it needs no import and runs nothing.

A name is public when it does not start with an underscore, or is a dunder such as `__init__`.
What counts:

- a function: its parameters with their kinds, annotations and defaults, its return annotation,
  its decorators, and whether it is `async`;
- a class: its bases, keywords and decorators, and then each public member, under
  `Class.member`, by the same rules;
- an assignment at module or class level: its name, and its annotation when it has one. The value
  is not compared: a changed constant is a change of behaviour, which the tests judge.

Imports are not counted, so a module may change what it imports from where. Names bound only
inside `if`, `try` or other blocks are not counted either.
"""

import ast
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

from tools._common import ToolError, git, resolve_inside, resolve_root

__all__ = ["check_public_api", "public_api"]


def public_api(source: str) -> dict[str, str]:
    """Return each public name defined at the top of the module `source`, with its signature.

    Raises `SyntaxError` when `source` does not parse.
    """
    return dict(_entries(ast.parse(source).body, ""))


def check_public_api(root: str | Path, paths: Sequence[str]) -> list[str]:
    """Return how the public API of each of `paths` differs from HEAD; empty when none does.

    `root` is a git work tree and each path a Python file relative to it. A path that is not in
    HEAD, has been deleted, or no longer parses is reported, not raised.
    """
    where = resolve_root(root)
    failures: list[str] = []
    for path in paths:
        try:
            rel = resolve_inside(where, path).as_posix()
        except ToolError as exc:
            failures.append(f"public_api: {path}: {exc.message}")
            continue
        failures.extend(_check_one(where, rel))
    return failures


def _check_one(root: Path, path: str) -> list[str]:
    blob = git(["show", f"HEAD:{path}"], root)
    if blob.returncode != 0:
        return [f"public_api: {path} is not in HEAD"]
    target = root / PurePosixPath(path)
    if not target.is_file():
        return [f"public_api: {path} was deleted"]
    try:
        before = public_api(blob.stdout.decode("utf-8", errors="replace"))
    except SyntaxError:
        return [f"public_api: {path} does not parse at HEAD"]
    try:
        after = public_api(target.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return [f"public_api: {path} no longer parses (line {exc.lineno})"]
    removed = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    changed = sorted(name for name in before.keys() & after.keys() if before[name] != after[name])
    if not (removed or added or changed):
        return []
    parts: list[str] = []
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    if added:
        parts.append(f"added {', '.join(added)}")
    parts.extend(f"changed {name} from `{before[name]}` to `{after[name]}`" for name in changed)
    return [f"public API of {path} changed: {'; '.join(parts)}"]


def _entries(body: list[ast.stmt], prefix: str) -> Iterator[tuple[str, str]]:
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _public(node.name):
            yield prefix + node.name, _function(node)
        elif isinstance(node, ast.ClassDef) and _public(node.name):
            yield prefix + node.name, _class(node)
            yield from _entries(node.body, f"{prefix}{node.name}.")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _names(target):
                    yield prefix + name, "assignment"
        elif isinstance(node, ast.AnnAssign):
            for name in _names(node.target):
                yield prefix + name, f"assignment: {ast.unparse(node.annotation)}"
        elif isinstance(node, ast.TypeAlias) and _public(node.name.id):
            yield prefix + node.name.id, f"type alias {ast.unparse(node.value)}"


def _names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name) and _public(target.id):
        yield target.id
    elif isinstance(target, ast.Tuple | ast.List):
        for item in target.elts:
            yield from _names(item)


def _function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    head = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{_decorators(node)}{head} {node.name}{_type_params(node)}({ast.unparse(node.args)}){returns}"


def _class(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    bases.extend(ast.unparse(keyword) for keyword in node.keywords)
    shown = f"({', '.join(bases)})" if bases else ""
    return f"{_decorators(node)}class {node.name}{_type_params(node)}{shown}"


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    return "".join(f"@{ast.unparse(decorator)} " for decorator in node.decorator_list)


def _type_params(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    params = [ast.unparse(param) for param in node.type_params]
    return f"[{', '.join(params)}]" if params else ""


def _public(name: str) -> bool:
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))
