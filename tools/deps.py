"""Read-only dependency tools: the import graph of a repository and the impact set of some paths.

`deps.graph` lists the Python, TypeScript and JavaScript modules of the work tree and the import
edges between them. Python is read with the standard `ast` module; TypeScript and JavaScript are
read with regular expressions over the source with its comments blanked out. `deps.impact` walks
the edges backwards from a set of paths and returns every module that imports one of them,
directly or through other modules, with the length of the shortest chain.

A module is named by its path relative to the repository root, so an edge is a pair of paths. The
files come from `git ls-files`, tracked or new but not ignored, so nothing outside the root and
nothing ignored is read. Symlinks are skipped, never followed. An import the tools cannot tie to
one file is never guessed: it is reported as unresolved, with a reason.

- `dynamic`: `importlib.import_module`, `__import__`, `import()` or `require` with an expression.
- `alias`: a TypeScript path alias, such as a key of `compilerOptions.paths` in the root
  `tsconfig.json` or `jsconfig.json`, `@/`, `~`, `#` or a bare name that starts with a top-level
  directory of the repository.
- `not_found`: a relative import, or an absolute Python import of a local package, with no file.
- `outside_root`: a relative import that climbs above the repository root.
- `absolute`: a TypeScript specifier that is an absolute file path.
- `ambiguous`: a Python import that two source roots both satisfy.

An import of a barrel file (a Python `__init__.py` or a TypeScript `index` file that re-exports) is
an edge to the barrel itself. Which re-exported module a name comes from is not resolved, but the
barrel's own re-exports are edges, so the impact set still reaches through it. Imports of the
standard library and of installed packages are external and are not edges.
"""

import ast
import json
import posixpath
import re
import sys
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.exit import ExitCode
from tools._common import (
    DEFAULT_MAX_BYTES,
    PathEscapeError,
    ToolInputError,
    ToolNotFoundError,
    fail_hint,
    git,
    resolve_inside,
    resolve_root,
    truncate,
)

MAX_FILE_BYTES = 1_048_576
"""Largest source file that is parsed; a bigger one is listed under `skipped` as `too_large`."""

PY_SUFFIXES = (".py",)
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_TS_TRY = (".ts", ".tsx", ".d.ts", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}

NODE_BUILTINS = frozenset(
    {
        "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants",
        "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2",
        "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode",
        "querystring", "readline", "repl", "stream", "string_decoder", "sys", "test", "timers",
        "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
    }
)  # fmt: skip
"""Node's built-in modules, which are external like any installed package."""


@dataclass(frozen=True, order=True)
class Edge:
    """`source` imports `target`, first at line `line` of `source`."""

    source: str
    target: str
    line: int


@dataclass(frozen=True, order=True)
class Unresolved:
    """An import at `line` of `source` whose `specifier` names no single file, and why."""

    source: str
    line: int
    specifier: str
    reason: str


@dataclass(frozen=True, order=True)
class External:
    """An import of the standard library or an installed package."""

    source: str
    line: int
    specifier: str


@dataclass
class Graph:
    """The import graph of one repository.

    `modules` maps each module's path to its language (`python`, `typescript`, `javascript`, or
    `other` for a non-source file such as a stylesheet that a module imports). `skipped` maps a
    source file that was not parsed to the reason (`symlink`, `too_large`, `syntax_error`).
    """

    root: Path
    modules: dict[str, str] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    external: list[External] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the graph as plain data with every list sorted, for `--json`."""
        return {
            "modules": [{"path": p, "language": lang} for p, lang in sorted(self.modules.items())],
            "edges": [vars(e) for e in sorted(self.edges)],
            "unresolved": [vars(u) for u in sorted(self.unresolved)],
            "external": [vars(x) for x in sorted(self.external)],
            "skipped": [{"path": p, "reason": r} for p, r in sorted(self.skipped.items())],
        }


@dataclass(frozen=True)
class _Import:
    """One import statement as read from a source file, before it is resolved."""

    line: int
    specifier: str
    dynamic: bool = False
    name: str = ""
    """The imported name of a Python `from specifier import name`; empty for any other import."""

    @property
    def shown(self) -> str:
        """Return the import as it is reported: the specifier, or `from x import y` in Python."""
        return f"from {self.specifier} import {self.name}" if self.name else self.specifier


# --- the tools --------------------------------------------------------------------------------


def graph(repo_path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Return the import graph of the work tree at `repo_path` as text, truncated to `max_bytes`.

    The first line counts the modules, edges, unresolved and external imports. Then each module
    with imports has one line, `path -> target, target`, and each unresolved import one line,
    `unresolved path:line 'specifier' (reason)`. Raises what `build_graph` raises.
    """
    return truncate(render_graph(build_graph(repo_path)).encode(), max_bytes)


def impact(
    repo_path: str | Path, paths: Sequence[str], *, max_bytes: int = DEFAULT_MAX_BYTES
) -> str:
    """Return the modules that import any of `paths`, directly or transitively, as text.

    Each line is `depth path`, where depth 1 is a direct importer; lines are sorted by depth, then
    path. A path is checked like any tool path and must be a module of the graph. Raises
    `ToolInputError` for an empty or malformed list, `PathEscapeError` for a path outside the
    root and `ToolNotFoundError` for a path that is not a module. When the repository has
    unresolved imports a final note says so, since the set may then be incomplete. The output is
    truncated to `max_bytes`.
    """
    g = build_graph(repo_path)
    targets = check_paths(g, paths)
    return truncate(render_impact(g, targets, impact_of(g, targets)).encode(), max_bytes)


# --- building the graph -------------------------------------------------------------------------


def build_graph(repo_path: str | Path) -> Graph:
    """Read every Python, TypeScript and JavaScript file under `repo_path` and return its graph.

    `repo_path` must be the top of a git work tree (`ToolNotFoundError` or `ToolInputError`
    otherwise). Only files `git ls-files` lists as tracked or new and not ignored are read, and a
    symlink is skipped. Nothing is written.
    """
    root = resolve_root(repo_path)
    proc = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    if proc.returncode != 0:
        raise ToolInputError("git ls-files failed", hint=fail_hint(proc))
    listed = sorted({p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p})
    g = Graph(root)
    files: set[str] = set()
    sources: list[tuple[str, str]] = []
    for path in listed:
        target = root / path
        if target.is_symlink():
            if _language(path):
                g.skipped[path] = "symlink"
            continue
        if not target.is_file():
            continue
        files.add(path)
        language = _language(path)
        if language is None:
            continue
        if target.stat().st_size > MAX_FILE_BYTES:
            g.skipped[path] = "too_large"
            continue
        sources.append((path, language))
    python = _PythonResolver({p for p in files if p.endswith(PY_SUFFIXES)})
    script = _ScriptResolver(root, files)
    edges: dict[tuple[str, str], int] = {}
    for path, language in sources:
        data = (root / path).read_bytes()
        if language == "python":
            try:
                imports = _python_imports(data)
            except SyntaxError, ValueError:
                g.skipped[path] = "syntax_error"
                continue
        else:
            imports = _script_imports(data.decode("utf-8", errors="replace"))
        g.modules[path] = language
        resolver = python.resolve if language == "python" else script.resolve
        for imp in imports:
            outcome = resolver(path, imp)
            if isinstance(outcome, str):
                if outcome != path:
                    edges.setdefault((path, outcome), imp.line)
            elif outcome[0] == "external":
                g.external.append(External(path, imp.line, imp.shown))
            else:
                g.unresolved.append(Unresolved(path, imp.line, imp.shown, outcome[1]))
    for (source, target), line in edges.items():
        g.modules.setdefault(target, _language(target) or "other")
        g.edges.append(Edge(source, target, line))
    return g


def _language(path: str) -> str | None:
    """Return the language of a source file by its suffix, or None for any other file."""
    if path.endswith(PY_SUFFIXES):
        return "python"
    if path.endswith((".ts", ".tsx", ".mts", ".cts")):
        return "typescript"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "javascript"
    return None


# An outcome is a resolved path, ("external", "") or ("unresolved", reason).
type _Outcome = str | tuple[str, str]
_EXTERNAL: _Outcome = ("external", "")


# --- Python -------------------------------------------------------------------------------------


def _python_imports(data: bytes) -> list[_Import]:
    """Return the imports of one Python file, read with `ast`; raises `SyntaxError` if it does not parse.

    `import a.b` gives `a.b`. `from a import b, c` gives `a` twice, with the names `b` and `c`, so
    the resolver can try `a.b` as a module before falling back to `a`. A relative import keeps its leading dots. A call
    to `importlib.import_module` or `__import__` is a dynamic import.
    """
    found: list[_Import] = []
    for node in ast.walk(ast.parse(data)):
        if isinstance(node, ast.Import):
            found += [_Import(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found += [_Import(node.lineno, base, name=alias.name) for alias in node.names]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None
                literal = isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                spec = arg.value if literal and isinstance(arg, ast.Constant) else "<expr>"
                found.append(_Import(node.lineno, str(spec), dynamic=True))
    return sorted(found, key=lambda i: i.line)


class _PythonResolver:
    """Ties a Python import to a file, using the source roots the repository's layout implies.

    The source roots are the repository root, `src/` and the parent of every top-level package
    (a directory with an `__init__.py` whose parent has none). A module outside any package also
    sees its own directory, as a script does.
    """

    def __init__(self, files: set[str]) -> None:
        self.files = files
        self.dirs = {posixpath.dirname(p) for p in files}
        self.dirs |= {d for p in list(self.dirs) for d in _ancestors(p)}
        packages = {posixpath.dirname(p) for p in files if posixpath.basename(p) == "__init__.py"}
        tops = {d for d in packages if posixpath.dirname(d) not in packages}
        self.roots = sorted({"", "src"} | {posixpath.dirname(d) for d in tops})
        self.packages = packages

    def resolve(self, source: str, imp: _Import) -> _Outcome:
        """Return the file `imp`, found in `source`, names, or why it names none."""
        if imp.dynamic:
            return ("unresolved", "dynamic")
        module, name = imp.specifier, imp.name
        if module.startswith("."):
            return self._relative(source, module, name)
        return self._absolute(source, module, name)

    def _relative(self, source: str, module: str, name: str) -> _Outcome:
        level = len(module) - len(module.lstrip("."))
        base = posixpath.dirname(source)
        for _ in range(level - 1):
            if not base:
                return ("unresolved", "outside_root")
            base = posixpath.dirname(base)
        parts = [p for p in module[level:].split(".") if p]
        found = self._under(base, [*parts, name]) if name and name != "*" else None
        found = found or self._under(base, parts)
        return found or ("unresolved", "not_found")

    def _absolute(self, source: str, module: str, name: str) -> _Outcome:
        parts = module.split(".")
        wanted = [[*parts, name], parts] if name and name != "*" else [parts]
        for roots in (self._own_roots(source), self.roots):
            for attempt in wanted:
                hits = {hit for r in roots if (hit := self._under(r, attempt))}
                if len(hits) == 1:
                    return hits.pop()
                if len(hits) > 1:
                    return ("unresolved", "ambiguous")
        if any(self._exists(r, parts[0]) for r in self.roots):
            return ("unresolved", "not_found")
        return _EXTERNAL

    def _own_roots(self, source: str) -> list[str]:
        """Return the roots that `source` itself sits under: its script directory or package root."""
        directory = posixpath.dirname(source)
        if directory not in self.packages:
            return [directory]
        while directory in self.packages:
            directory = posixpath.dirname(directory)
        return [directory]

    def _under(self, base: str, parts: list[str]) -> str | None:
        """Return the file of the dotted `parts` below `base`, a module or a package, or None."""
        stem = posixpath.join(base, *parts) if parts else base
        for candidate in (f"{stem}.py", posixpath.join(stem, "__init__.py")):
            if parts and candidate in self.files:
                return candidate
        if not parts and posixpath.join(base, "__init__.py") in self.files:
            return posixpath.join(base, "__init__.py")
        return None

    def _exists(self, root: str, top: str) -> bool:
        """Say whether `top` is a local module or directory under the source root `root`."""
        stem = posixpath.join(root, top)
        return f"{stem}.py" in self.files or stem in self.dirs


def _ancestors(path: str) -> Iterable[str]:
    """Yield every proper ancestor directory of the relative `path`, the root `""` included."""
    while path:
        path = posixpath.dirname(path)
        yield path


# --- TypeScript and JavaScript ------------------------------------------------------------------

_NOT_IMPORT_OR_EXPORT = r"(?:(?!\b(?:import|export)\b)[^'\";])*?"
_STATIC = re.compile(
    rf"(?<![\w$.])(?:import(?!\s*[(.]){_NOT_IMPORT_OR_EXPORT}\bfrom|import(?!\s*[(.])"
    rf"|export{_NOT_IMPORT_OR_EXPORT}\bfrom)\s*(['\"])([^'\"\n]*)\1"
)
_CALL = re.compile(r"(?<![\w$.])(require|import)\s*\(\s*(?:(['\"])([^'\"\n]*)\2\s*\))?")


def _blank_comments(text: str) -> str:
    """Return `text` with `//` and `/* */` comments replaced by spaces, newlines kept.

    String and template literals are skipped over so that `//` inside one is not a comment. A
    quoted string ends at a newline, which keeps a regular expression literal holding a quote
    from swallowing the rest of the file. Offsets and line numbers are unchanged.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        elif c in "'\"`":
            i += 1
            while i < n and text[i] != c and (c == "`" or text[i] != "\n"):
                i += 2 if text[i] == "\\" else 1
            i += 1
        else:
            i += 1
    return "".join(out)


def _script_imports(text: str) -> list[_Import]:
    """Return the imports of one TypeScript or JavaScript file.

    Static forms are `import ... from 'x'`, `import 'x'`, `export ... from 'x'` and `require('x')`
    (which covers `import x = require('x')`). `import(...)`, and `require` with anything but one
    string literal, are dynamic. Text that looks like an import inside a string can be misread.
    """
    code = _blank_comments(text)
    found: dict[int, _Import] = {}

    def line(offset: int) -> int:
        return code.count("\n", 0, offset) + 1

    for m in _STATIC.finditer(code):
        found[m.start()] = _Import(line(m.start()), m.group(2))
    for m in _CALL.finditer(code):
        if m.start() in found:
            continue
        if m.group(1) == "require" and m.group(3) is not None:
            found[m.start()] = _Import(line(m.start()), m.group(3))
        else:
            spec = m.group(3) if m.group(3) is not None else "<expr>"
            found[m.start()] = _Import(line(m.start()), spec, dynamic=True)
    return [found[k] for k in sorted(found)]


class _ScriptResolver:
    """Ties a TypeScript or JavaScript specifier to a file the way a bundler-style resolver does.

    A relative specifier is tried as written, with each source suffix, with `.js` swapped for
    `.ts`, and as a directory `index` file. A bare specifier is external unless it looks like a
    path alias. Nothing is read but the root `tsconfig.json` and `jsconfig.json`.
    """

    def __init__(self, root: Path, files: set[str]) -> None:
        self.files = files
        self.top_dirs = {p.split("/", 1)[0] for p in files if "/" in p}
        self.aliases = _config_aliases(root, files)

    def resolve(self, source: str, imp: _Import) -> _Outcome:
        """Return the file `imp`, found in `source`, names, or why it names none."""
        spec = imp.specifier
        if imp.dynamic:
            return ("unresolved", "dynamic")
        if spec.startswith("/"):
            return ("unresolved", "absolute")
        if spec in (".", "..") or spec.startswith(("./", "../")):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(source), spec))
            if joined == ".." or joined.startswith("../"):
                return ("unresolved", "outside_root")
            return self._file(joined) or ("unresolved", "not_found")
        if spec.startswith("node:") or spec.split("/", 1)[0] in NODE_BUILTINS:
            return _EXTERNAL
        if self._is_alias(spec):
            return ("unresolved", "alias")
        return _EXTERNAL

    def _file(self, stem: str) -> str | None:
        """Return the file the relative path `stem` names, or None."""
        stem = "" if stem == "." else stem
        candidates = [stem] if stem else []
        candidates += [stem + suffix for suffix in _TS_TRY]
        base, suffix = posixpath.splitext(stem)
        candidates += [base + ts for ts in _JS_TO_TS.get(suffix, ())]
        candidates += [posixpath.join(stem, "index" + suffix) for suffix in _TS_TRY]
        return next((c for c in candidates if c in self.files), None)

    def _is_alias(self, spec: str) -> bool:
        """Say whether a bare specifier is a path alias rather than an installed package."""
        if spec.startswith(("@/", "~", "#")):
            return True
        if any(spec == a or (a.endswith("*") and spec.startswith(a[:-1])) for a in self.aliases):
            return True
        return spec.split("/", 1)[0] in self.top_dirs and not spec.startswith("@")


def _config_aliases(root: Path, files: set[str]) -> list[str]:
    """Return the keys of `compilerOptions.paths` in the root `tsconfig.json` and `jsconfig.json`.

    The file is JSON with comments and trailing commas allowed; one that still does not parse
    contributes nothing.
    """
    keys: list[str] = []
    for name in ("tsconfig.json", "jsconfig.json"):
        if name not in files or (root / name).stat().st_size > MAX_FILE_BYTES:
            continue
        text = _blank_comments((root / name).read_text(encoding="utf-8", errors="replace"))
        try:
            config = json.loads(re.sub(r",(\s*[}\]])", r"\1", text))
            paths = config.get("compilerOptions", {}).get("paths", {})
        except ValueError, AttributeError:
            continue
        if isinstance(paths, dict):
            keys += [k for k in paths if isinstance(k, str)]
    return keys


# --- the impact set -----------------------------------------------------------------------------


def check_paths(g: Graph, paths: Sequence[str]) -> list[str]:
    """Return `paths` as module paths of `g`, relative to its root, in order and without repeats.

    Raises `ToolInputError` when `paths` is not a non-empty list of strings, `PathEscapeError`
    when one leaves the root and `ToolNotFoundError` when one is not a module of the graph.
    """
    if isinstance(paths, str) or not isinstance(paths, Sequence) or not paths:
        raise ToolInputError("paths must be a non-empty list of file paths")
    checked: list[str] = []
    for path in paths:
        rel = resolve_inside(g.root, path).as_posix()
        if rel not in g.modules:
            raise ToolNotFoundError(
                f"not a module of the import graph: {path!r}",
                hint="give a Python, TypeScript or JavaScript file in the work tree",
            )
        if rel not in checked:
            checked.append(rel)
    return checked


def impact_of(g: Graph, targets: Iterable[str]) -> list[tuple[str, int]]:
    """Return `(path, depth)` for every module that imports one of `targets`, transitively.

    The depth is the number of edges on the shortest chain to the nearest target, so a direct
    importer has depth 1. The targets themselves are left out, and a cycle is walked once. The
    result is sorted by depth, then path.
    """
    importers: dict[str, set[str]] = {}
    for edge in g.edges:
        importers.setdefault(edge.target, set()).add(edge.source)
    depth = dict.fromkeys(targets, 0)
    queue = deque(depth)
    while queue:
        current = queue.popleft()
        for importer in sorted(importers.get(current, ())):
            if importer not in depth:
                depth[importer] = depth[current] + 1
                queue.append(importer)
    return sorted(((p, d) for p, d in depth.items() if d > 0), key=lambda item: (item[1], item[0]))


# --- rendering and the command ------------------------------------------------------------------


def render_graph(g: Graph) -> str:
    """Return the text form of `g` that `graph` returns and `factory deps` prints."""
    counts = (
        f"{len(g.modules)} modules, {len(g.edges)} edges, {len(g.unresolved)} unresolved, "
        f"{len(g.external)} external"
    )
    lines = [counts]
    targets: dict[str, list[str]] = {}
    for edge in sorted(g.edges):
        targets.setdefault(edge.source, []).append(edge.target)
    lines += [f"{source} -> {', '.join(t)}" for source, t in targets.items()]
    lines += [
        f"unresolved {u.source}:{u.line} {u.specifier!r} ({u.reason})" for u in sorted(g.unresolved)
    ]
    lines += [f"skipped {path} ({reason})" for path, reason in sorted(g.skipped.items())]
    return "\n".join(lines) + "\n"


def render_impact(g: Graph, targets: Sequence[str], impacted: Sequence[tuple[str, int]]) -> str:
    """Return the text form of an impact set that `impact` returns and `factory deps` prints."""
    lines = [f"{len(impacted)} modules import {', '.join(targets)}"]
    lines += [f"{depth} {path}" for path, depth in impacted]
    if g.unresolved:
        lines.append(
            f"note: {len(g.unresolved)} unresolved imports in the repository, "
            "so the set may be incomplete"
        )
    return "\n".join(lines) + "\n"


def command(repo_path: str, *, impact_paths: Sequence[str] | None, as_json: bool) -> int:
    """Print the import graph of `repo_path`, and the impact set of `impact_paths` when given.

    Under `as_json`, stdout gets one document: the keys of `Graph.to_dict` and `impact`, which is
    null without `impact_paths` and otherwise `{"paths", "impacted": [{"path", "depth"}]}`.
    Without it, the text forms of `render_graph` and, when asked, `render_impact` are printed.
    Returns 0; failures raise the tools' errors, which the CLI maps to exit codes.
    """
    g = build_graph(repo_path)
    targets = check_paths(g, impact_paths) if impact_paths else None
    impacted = impact_of(g, targets) if targets is not None else None
    if as_json:
        document = g.to_dict()
        document["impact"] = (
            None
            if targets is None or impacted is None
            else {
                "paths": targets,
                "impacted": [{"path": p, "depth": d} for p, d in impacted],
            }
        )
        print(json.dumps(document))
    elif targets is not None and impacted is not None:
        sys.stdout.write(render_impact(g, targets, impacted))
    else:
        sys.stdout.write(render_graph(g))
    return ExitCode.OK


__all__ = [
    "Edge",
    "External",
    "Graph",
    "PathEscapeError",
    "Unresolved",
    "build_graph",
    "check_paths",
    "command",
    "graph",
    "impact",
    "impact_of",
    "render_graph",
    "render_impact",
]
