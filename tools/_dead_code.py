"""Static dead code: `deps.dead_code`, the symbols and modules of a repository nothing appears to use.

Nothing read is imported or run, and the report never depends on a setting the repository holds.
Python is read with `ast`. Vulture, used as a library, finds the definitions whose bare name no
file uses; its command line, which is the part that reads `[tool.vulture]`, is never entered, its
`# noqa` handling is switched off and whitelist modules are set aside, so a file in the repository
cannot silence a finding. Three rules go further than vulture and repeat until nothing new appears:

- `private`: a module-level definition whose name starts with one underscore, that its own module
  never uses and no other module imports or reads as an attribute, even when another module has a
  live definition of the same name.
- `module`: a module nothing in the tree imports, from the `deps.graph` import graph. It is reported
  once and its definitions are part of it. `__init__.py`, `__main__.py`, `setup.py`, a module with
  an `if __name__ == "__main__"` block, test files and `conftest.py` are entry points, never dead.
- `through_dead`: a definition or module that only other dead code uses.

A use in a test file is a use, and a name an `__init__.py` re-exports is in use. A definition that
no static use reaches, but that a dynamic lookup may, is not called dead: it goes in a separate list
with the path and line of that lookup. See `docs/CONVENTIONS.md` (Tools) for the limits.

TypeScript and JavaScript get a smaller report, with no toolchain: exported names that no other file
in the tree imports, read with regular expressions like the rest of `tools.deps`.
"""

import ast
import builtins
import posixpath
import re
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from vulture.core import Vulture

from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolNotFoundError,
    ToolSpec,
    resolve_inside,
    truncate,
)

if TYPE_CHECKING:
    from tools.deps import Graph, _Import

CONFIDENCE = {"vulture": 60, "private": 80, "module": 60, "through_dead": 60}
"""The confidence of a dead finding by the rule that found it, as a percentage like vulture's own."""

DYNAMIC_CONFIDENCE = 20
"""The confidence of a definition that a dynamic lookup may reach."""

EXPORT_CONFIDENCE = 40
"""The confidence of an unused TypeScript or JavaScript export: its reading is the coarsest."""

MAX_DEPTH = 200
"""A Python file whose syntax tree is deeper is skipped as `too_deep`: vulture's visitor recurses."""

NEUTRAL_DECORATORS = frozenset(
    [
        "abstractmethod",
        "asynccontextmanager",
        "cache",
        "cached_property",
        "classmethod",
        "contextmanager",
        "dataclass",
        "deleter",
        "deprecated",
        "final",
        "getter",
        "lru_cache",
        "no_type_check",
        "overload",
        "override",
        "property",
        "runtime_checkable",
        "setter",
        "singledispatch",
        "singledispatchmethod",
        "staticmethod",
        "total_ordering",
        "unique",
        "wraps",
    ]
)
"""Decorators known not to hand the definition to a registry; any other one may."""

NEUTRAL_BASES = frozenset(
    [
        "ABC",
        "ABCMeta",
        "Enum",
        "Flag",
        "Generic",
        "IntEnum",
        "IntFlag",
        "NamedTuple",
        "Protocol",
        "StrEnum",
        "TypedDict",
    ]
)
"""Base classes known not to call a subclass's methods by name or to register the subclass."""

type _Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_DOTTED = re.compile(r"\w+(?:\.\w+)*(?::\w+(?:\.\w+)*)?\Z")
_IMPORTERS = ("import_module", "__import__", "run_module", "run_path", "spec_from_file_location")


@dataclass(frozen=True, order=True)
class Finding:
    """One symbol reported: `symbol` is `path::Qualified.name`, or the path alone for a module.

    `rule` names what found it: `vulture`, `private`, `module`, `through_dead` or `export`. `detail`
    says why in a few words; in the dynamic list it names the lookup as `path:line in symbol: text`.
    """

    path: str
    line: int
    symbol: str
    kind: str
    confidence: int
    rule: str
    detail: str


@dataclass
class Report:
    """What `analyse` found. Every list is sorted by path and line."""

    dead: list[Finding] = field(default_factory=list)
    dynamic: list[Finding] = field(default_factory=list)
    """No static use, but a dynamic lookup may reach it; `detail` is the evidence."""
    exports: list[Finding] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    python: int = 0
    scripts: int = 0


def dead_code(
    repo_path: str | Path, path: str | None = None, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> str:
    """Return the dead code of the repository at `repo_path` as text, at most `max_bytes` of it.

    The first line counts the findings. Then come the dead symbols, the symbols a dynamic lookup may
    reach and the unused TypeScript or JavaScript exports, one line each, sorted by path and line,
    and last one `not scanned: path (reason)` line per skipped file. The whole tree is always read;
    `path` limits what is listed to that file or directory. A `path` outside the root raises
    `PathEscapeError`, one that does not exist `ToolNotFoundError`, and a `repo_path` that is not
    the top of a git work tree what `resolve_root` raises. Nothing is written or run.
    """
    return truncate(render(analyse(repo_path, path)).encode(), max_bytes)


def render(report: Report) -> str:
    """Return the text form of `report` that `dead_code` returns."""
    counts = (
        f"dead code: {len(report.dead)} dead, {len(report.dynamic)} a dynamic lookup may reach, "
        f"{len(report.exports)} unused exports; {report.python} Python and {report.scripts} "
        "TypeScript or JavaScript files read"
    )
    lines = [counts]
    sections = (
        ("dead:", report.dead),
        ("not called dead, a dynamic lookup may reach it:", report.dynamic),
        ("exports no other file imports:", report.exports),
    )
    for title, findings in sections:
        if findings:
            lines.append(title)
        for f in findings:
            where = f"{f.path}:{f.line}: {f.symbol}"
            lines.append(f"{where} ({f.kind}, confidence {f.confidence}, {f.rule}): {f.detail}")
    lines += [f"not scanned: {p} ({reason})" for p, reason in sorted(report.skipped.items())]
    return "\n".join(lines) + "\n"


def analyse(repo_path: str | Path, path: str | None = None) -> Report:
    """Return what `dead_code` reports, as data. Raises what `dead_code` raises."""
    from tools import deps  # `tools.deps` imports this module, so the import waits for a call

    g = deps.build_graph(repo_path)
    wanted = ""
    if path is not None:
        wanted = resolve_inside(g.root, path).as_posix()
        if not (g.root / wanted).exists():
            raise ToolNotFoundError(f"no file or directory named {path!r}")
    report = Report(skipped=dict(g.skipped))
    _Python(g, report).run()
    _Scripts(g, report).run()

    def listed(found: str) -> bool:
        return wanted in ("", ".") or found == wanted or found.startswith(wanted + "/")

    report.dead = sorted(f for f in report.dead if listed(f.path))
    report.dynamic = sorted(f for f in report.dynamic if listed(f.path))
    report.exports = sorted(f for f in report.exports if listed(f.path))
    report.skipped = {p: reason for p, reason in report.skipped.items() if listed(p)}
    return report


# --- Python: reading ----------------------------------------------------------------------------


@dataclass
class _Module:
    """One parsed Python file. `tree` loses the definitions found dead as the rules repeat."""

    path: str
    tree: ast.Module
    test: bool

    @property
    def dotted(self) -> list[str]:
        """Return the components of the module's dotted name, seen from the repository root."""
        parts = self.path.removesuffix(".py").split("/")
        return parts[:-1] if parts[-1] == "__init__" and len(parts) > 1 else parts


@dataclass(eq=False)
class _Def:
    """One function, class or method as it stands in a module's tree."""

    module: _Module
    node: _Definition
    qual: str
    owner: ast.ClassDef | None
    """The class whose body holds the definition."""
    top: bool
    """Whether the definition is a name of the module itself."""
    inside: tuple[int, ...]
    """The `id` of every definition around this one."""

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def symbol(self) -> str:
        return f"{self.module.path}::{self.qual}"

    @property
    def kind(self) -> str:
        if isinstance(self.node, ast.ClassDef):
            return "class"
        return "function" if self.owner is None else "method"


@dataclass(frozen=True)
class _Lookup:
    """One place that picks a name at run time: where it is and what it may reach."""

    path: str
    line: int
    symbol: str
    text: str
    scope: str
    """`any`, `module`, `modules` (names of modules), `class`, `import` (a module) or `string`."""
    target: str = ""
    """The module path, class name or whole string the scope refers to."""
    prefix: str = ""
    suffix: str = ""
    exact: bool = False

    @property
    def evidence(self) -> str:
        where = f"{self.path}:{self.line}"
        return f"{where} in {self.symbol}: {self.text}" if self.symbol else f"{where}: {self.text}"

    def matches(self, name: str) -> bool:
        """Say whether the fixed text of the looked-up name allows `name`."""
        if self.exact:
            return name == self.prefix
        fixed = len(self.prefix) + len(self.suffix)
        return name.startswith(self.prefix) and name.endswith(self.suffix) and len(name) >= fixed


def _is_test(path: str) -> bool:
    """Say whether a Python path is a test file: pytest's names, or anything below a tests directory."""
    *dirs, name = path.split("/")
    named = name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    return named or "tests" in dirs or "test" in dirs


def _parse(data: bytes) -> ast.Module:
    """Parse one file that `build_graph` already parsed, keeping the type comments that are valid."""
    try:
        tree = ast.parse(data, type_comments=True)
    except SyntaxError:
        return ast.parse(data)
    for node in ast.walk(tree):
        comment = getattr(node, "type_comment", None)
        if comment is None:
            continue
        mode = "func_type" if isinstance(node, _DEFINITIONS) else "eval"
        try:
            ast.parse(comment, mode=mode)
        except SyntaxError:
            node.type_comment = None  # ty: ignore[unresolved-attribute]
    return tree


def _too_deep(tree: ast.Module) -> bool:
    """Say whether `tree` is nested deeper than `MAX_DEPTH`."""
    level: list[ast.AST] = [tree]
    for _ in range(MAX_DEPTH):
        level = [child for node in level for child in ast.iter_child_nodes(node)]
        if not level:
            return False
    return True


def _is_bare(stmt: ast.stmt) -> bool:
    """Say whether a statement only names something, as the lines of a vulture whitelist do."""
    if not isinstance(stmt, ast.Expr):
        return False
    value = stmt.value
    while isinstance(value, ast.Attribute):
        value = value.value
    return isinstance(value, ast.Name)


def _is_whitelist(tree: ast.Module) -> bool:
    """Say whether a module holds nothing but imports and bare names, at least one of them."""
    body = [s for s in tree.body if not isinstance(s, ast.Import | ast.ImportFrom)]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return bool(body) and all(_is_bare(s) for s in body)


def _has_main_block(tree: ast.Module) -> bool:
    """Say whether a module has a top-level `if __name__ == "__main__"` block."""
    for stmt in tree.body:
        if isinstance(stmt, ast.If):
            names = {n.id for n in ast.walk(stmt.test) if isinstance(n, ast.Name)}
            values = {n.value for n in ast.walk(stmt.test) if isinstance(n, ast.Constant)}
            if "__name__" in names and "__main__" in values:
                return True
    return False


def _definitions(module: _Module) -> list[_Def]:
    """Return every definition in a module's tree, in no particular order."""
    found: list[_Def] = []
    stack: list[tuple[ast.AST, str, ast.AST, tuple[int, ...]]] = [
        (module.tree, "", module.tree, ())
    ]
    while stack:
        node, prefix, scope, inside = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEFINITIONS):
                owner = scope if isinstance(scope, ast.ClassDef) else None
                top = scope is module.tree
                found.append(_Def(module, child, prefix + child.name, owner, top, inside))
                below = (*inside, id(child))
                stack.append((child, f"{prefix}{child.name}.", child, below))
            else:
                stack.append((child, prefix, scope, inside))
    return found


def _remove(tree: ast.AST, doomed: set[int]) -> None:
    """Take the statements whose `id` is in `doomed` out of `tree`, leaving `pass` in an empty body."""
    for node in ast.walk(tree):
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and any(id(item) in doomed for item in value):
                line = next(item.lineno for item in value if id(item) in doomed)
                value[:] = [item for item in value if id(item) not in doomed]
                value[:] = value or [ast.Pass(lineno=line, col_offset=0)]


def _dotted(node: ast.AST) -> str:
    """Return `a.b.c` for a name or a chain of attributes on a name, else an empty string."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    return ".".join([node.id, *reversed(parts)])


def _last(node: ast.AST) -> str:
    """Return the last component of the name a decorator, a base or a callee refers to."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Subscript):
        node = node.value
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def _literal(node: ast.AST | None) -> str | None:
    """Return the value of a string literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _fixed_text(node: ast.AST) -> tuple[str, str]:
    """Return the fixed start and end of a string built at run time, empty where there is none."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        start = _fixed_text(node.left)[0] if left is None else left
        end = _fixed_text(node.right)[1] if right is None else right
        return start, end
    if isinstance(node, ast.JoinedStr) and node.values:
        return _literal(node.values[0]) or "", _literal(node.values[-1]) or ""
    template, pattern = None, ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template, pattern = _literal(node.left), r"%[^a-zA-Z%]*[a-zA-Z]"
    elif isinstance(node, ast.Call) and _last(node.func) == "format":
        func = node.func
        template = _literal(func.value) if isinstance(func, ast.Attribute) else None
        pattern = r"\{[^{}]*\}"
    if not template or any(escape in template for escape in ("%%", "{{", "}}")):
        return "", ""
    pieces = re.split(pattern, template)
    return (pieces[0], pieces[-1]) if len(pieces) > 1 else ("", "")


# --- Python: what one module uses ---------------------------------------------------------------


@dataclass
class _Facts:
    """What one module's current tree says, for the rules vulture has no part in."""

    loads: Counter[str] = field(default_factory=Counter)
    """Names and attributes read, counted."""
    annotated: set[str] = field(default_factory=set)
    """Names read inside a string annotation, which vulture does not read."""
    imported: dict[tuple[str, str], None] = field(default_factory=dict)
    """`(target path, name)` of every `from` import; the target is empty when it is not resolved."""
    attributes: set[tuple[str, str]] = field(default_factory=set)
    """`(module path, name)` of every attribute read; the path is empty for an unknown object."""
    bound: dict[str, tuple[str, str]] = field(default_factory=dict)
    """The local name of each `from` import, with the target path and the name there."""
    stars: set[str] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)
    """The files an import by literal name, such as `import_module("a.b")`, resolves to."""
    escaped: set[str] = field(default_factory=set)
    """The modules this one passes around as a value."""
    lookups: list[_Lookup] = field(default_factory=list)
    subclassed: list[_Lookup] = field(default_factory=list)
    """Every read of `__subclasses__`, with the class name as the target."""


class _Reader:
    """Collects the `_Facts` of one module, without recursion."""

    def __init__(self, module: _Module, resolve: _Resolve, classes: set[str]) -> None:
        self.module = module
        self.resolve = resolve
        self.classes = classes
        self.facts = _Facts()
        self.aliases: dict[str, str] = {}
        self.parents: dict[ast.AST, ast.AST] = {}

    def read(self) -> _Facts:
        """Read the whole tree."""
        nodes = list(ast.walk(self.module.tree))
        for node in nodes:
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom):
                self._import(node)
        self._drop_rebound(nodes)
        for node in nodes:
            self._node(node)
        return self.facts

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            if isinstance(node, ast.Import):
                found = self.resolve(self.module.path, node.lineno, alias.name, "")
                if found:
                    self.aliases[alias.asname or alias.name] = found
                continue
            base = "." * node.level + (node.module or "")
            found = self.resolve(self.module.path, node.lineno, base, alias.name)
            if found is None:
                continue
            if alias.name == "*":
                self.facts.stars.add(found)
                continue
            holder = self.resolve(self.module.path, node.lineno, base, "")
            if found and found != holder:
                self.aliases[alias.asname or alias.name] = found
                continue
            self.facts.imported[(found, alias.name)] = None
            self.facts.bound[alias.asname or alias.name] = (found, alias.name)

    def _drop_rebound(self, nodes: Iterable[ast.AST]) -> None:
        """Forget an alias that the module also binds some other way: it may hold another object."""
        for node in nodes:
            name = ""
            if isinstance(node, ast.arg):
                name = node.arg
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id
            elif isinstance(node, _DEFINITIONS):
                name = node.name
            if name in self.aliases:
                self.facts.escaped.add(self.aliases.pop(name))

    def _node(self, node: ast.AST) -> None:
        facts = self.facts
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            facts.loads[node.id] += 1
            if node.id in self.aliases and not self._only_looked_into(node):
                facts.escaped.add(self.aliases[node.id])
        elif isinstance(node, ast.Attribute):
            self._attribute(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            names = len(text) <= 200 and re.search(r"[.:]", text)
            if names and (_DOTTED.match(text) or text.endswith(".py")):
                self._add(node, "string", text)
        elif isinstance(node, ast.Call):
            self._call(node)
        elif isinstance(node, ast.Assign | ast.AugAssign):
            self._assign(node)
        for annotation in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if isinstance(annotation, ast.AST):
                self._annotation(annotation)

    def _only_looked_into(self, node: ast.Name) -> bool:
        """Say whether a module's name is only used to reach an attribute, not passed on."""
        parent = self.parents.get(node)
        if isinstance(parent, ast.Attribute):
            return True
        names = ("getattr", "hasattr", "dir", "vars", "getmembers")
        first = isinstance(parent, ast.Call) and parent.args[:1] == [node]
        return first and isinstance(parent, ast.Call) and _last(parent.func) in names

    def _attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Store):
            return
        self.facts.loads[node.attr] += 1
        base = _dotted(node.value)
        if base not in ("self", "cls"):
            self.facts.attributes.add((self.aliases.get(base, ""), node.attr))
        if node.attr == "__dict__" and base in self.aliases:
            self._add(node, "module", self.aliases[base])
        elif node.attr == "__dict__" and base in self.classes:
            self._add(node, "class", base)
        elif node.attr == "__subclasses__":
            self.facts.subclassed.append(
                replace(self._lookup(node, "class"), target=_last(node.value))
            )

    def _assign(self, node: ast.Assign | ast.AugAssign) -> None:
        """Count the strings of an `__all__` as reads of the module's own names."""
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            for item in ast.walk(node.value):
                if (text := _literal(item)) is not None:
                    self.facts.loads[text] += 1

    def _annotation(self, annotation: ast.AST) -> None:
        for item in ast.walk(annotation):
            text = _literal(item)
            if text is None:
                continue
            try:
                tree = ast.parse(text, mode="eval")
            except SyntaxError, ValueError:
                continue
            for name in ast.walk(tree):
                if isinstance(name, ast.Name | ast.Attribute):
                    self.facts.loads[_last(name)] += 1
                    self.facts.annotated.add(_last(name))

    def _call(self, node: ast.Call) -> None:
        name, args = _last(node.func), node.args
        if name in ("getattr", "hasattr") and len(args) >= 2:
            text = _literal(args[1])
            if text is not None:
                self.facts.loads[text] += 1
                self.facts.attributes.add((self.aliases.get(_dotted(args[0]), ""), text))
            elif name == "getattr":
                self._reach(node, args[0], args[1])
        elif (
            name in ("dir", "getmembers")
            and args
            or name == "vars"
            and args
            and _dotted(args[0]) in self.aliases
        ):
            self._reach(node, args[0], None)
        elif name in ("attrgetter", "methodcaller"):
            for arg in args if name == "attrgetter" else args[:1]:
                for part in (_literal(arg) or "").split("."):
                    if part:
                        self._add(node, "any", prefix=part, exact=True)
                if _literal(arg) is None:
                    start, end = _fixed_text(arg)
                    self._add(node, "any", prefix=start, suffix=end)
        elif name == "globals":
            self._globals(node)
        elif name in _IMPORTERS and args:
            self._importer(node, name, args[0])

    def _globals(self, node: ast.Call) -> None:
        parent = self.parents.get(node)
        key: ast.AST | None = None
        if isinstance(parent, ast.Subscript):
            key = parent.slice
        elif isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = self.parents.get(parent)
            key = call.args[0] if isinstance(call, ast.Call) and call.args else None
        text = _literal(key)
        if text is not None:
            self._add(node, "module", self.module.path, prefix=text, exact=True)
            return
        start, end = _fixed_text(key) if key is not None else ("", "")
        self._add(parent or node, "module", self.module.path, prefix=start, suffix=end)

    def _importer(self, node: ast.Call, name: str, arg: ast.AST) -> None:
        text = _literal(arg)
        if text is None:
            start, end = _fixed_text(arg) if name in _IMPORTERS[:2] else ("", "")
            self._add(node, "import", prefix=start, suffix=end)
            return
        for source in (self.module.path, "") if text.startswith(".") else ("",):
            found = self.resolve(source or self.module.path, node.lineno, text, "")
            if found:
                self.facts.modules.add(found)
                self.facts.escaped.add(found)

    def _reach(self, node: ast.Call, receiver: ast.AST, key: ast.AST | None) -> None:
        """Record a lookup of a run-time name on `receiver`."""
        start, end = _fixed_text(key) if key is not None else ("", "")
        scope, target = self._receiver(receiver)
        self._add(node, scope, target, prefix=start, suffix=end)

    def _receiver(self, node: ast.AST) -> tuple[str, str]:
        """Return the scope of a lookup on `node` and the module or class it is limited to."""
        base = _dotted(node)
        if base in self.aliases:
            return "module", self.aliases[base]
        around = self._class_around(node)
        own = base in ("self", "cls", "self.__class__") or (
            isinstance(node, ast.Call) and _last(node.func) in ("super", "type")
        )
        if own and around:
            return "class", around
        if base in self.classes:
            return "class", base
        if isinstance(node, ast.Call) and _last(node.func) in _IMPORTERS:
            return "modules", ""
        if isinstance(node, ast.Subscript) and _dotted(node.value) == "sys.modules":
            own_name = isinstance(node.slice, ast.Name) and node.slice.id == "__name__"
            return ("module", self.module.path) if own_name else ("modules", "")
        return "any", ""

    def _around(self, node: ast.AST) -> list[_Definition]:
        """Return the definitions around `node`, the outermost first."""
        found: list[_Definition] = []
        at: ast.AST | None = node
        while (at := self.parents.get(at)) is not None:
            if isinstance(at, _DEFINITIONS):
                found.append(at)
        return found[::-1]

    def _class_around(self, node: ast.AST) -> str:
        classes = [d.name for d in self._around(node) if isinstance(d, ast.ClassDef)]
        return classes[-1] if classes else ""

    def _lookup(self, node: ast.AST, scope: str) -> _Lookup:
        around = ".".join(d.name for d in self._around(node))
        symbol = f"{self.module.path}::{around}" if around else ""
        text = " ".join(ast.unparse(node).split())
        line = getattr(node, "lineno", 1)
        return _Lookup(self.module.path, line, symbol, text[:120], scope)

    def _add(
        self, node: ast.AST, scope: str, target: str = "", *,
        prefix: str = "", suffix: str = "", exact: bool = False,
    ) -> None:  # fmt: skip
        self.facts.lookups.append(
            replace(
                self._lookup(node, scope),
                target=target, prefix=prefix, suffix=suffix, exact=exact,
            )
        )  # fmt: skip


class _Resolve:
    """Resolves a Python import the way `tools.deps` does: a path, "" if unresolved, None if external."""

    def __init__(self, files: set[str]) -> None:
        from tools import deps

        self.resolver = deps._PythonResolver(files)
        self.make: type[_Import] = deps._Import

    def __call__(self, source: str, line: int, specifier: str, name: str) -> str | None:
        outcome = self.resolver.resolve(source, self.make(line, specifier, name=name))
        if isinstance(outcome, str):
            return outcome
        return None if outcome[0] == "external" else ""


def _entry_points(root: Path, skipped: dict[str, str]) -> Iterator[_Lookup]:
    """Yield the `package.module:name` strings of the root `pyproject.toml` that name an entry point."""
    path = root / "pyproject.toml"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return

    def table(*keys: str) -> dict[str, object]:
        at: object = config
        for key in keys:
            at = at.get(key) if isinstance(at, dict) else None
        return {str(k): v for k, v in at.items()} if isinstance(at, dict) else {}

    groups = [table("project", "scripts"), table("project", "gui-scripts")]
    groups += [table("tool", "poetry", "scripts")]
    groups += [table("project", "entry-points", name) for name in table("project", "entry-points")]
    lines = text.split("\n")
    for value in (v for group in groups for v in group.values() if isinstance(v, str)):
        spec = value.split("[")[0].strip()
        if _DOTTED.match(spec):
            line = next((n for n, row in enumerate(lines, 1) if value in row), 1)
            yield _Lookup("pyproject.toml", line, "", f"entry point {value!r}", "string", spec)


# --- Python: the rules --------------------------------------------------------------------------


class _Python:
    """Runs vulture and the rules over the Python modules of a graph until nothing new is dead."""

    def __init__(self, g: Graph, report: Report) -> None:
        self.g = g
        self.report = report
        self.modules: dict[str, _Module] = {}
        self.whitelists: set[str] = set()
        paths = {p for p, language in g.modules.items() if language == "python"}
        known = paths | {p for p, r in g.skipped.items() if p.endswith(".py") and r != "symlink"}
        self.resolve = _Resolve(known)
        for path in sorted(paths - set(g.skipped)):
            tree = _parse((g.root / path).read_bytes())
            if _is_whitelist(tree):
                report.skipped[path] = "whitelist"
                self.whitelists.add(path)
            elif _too_deep(tree):
                report.skipped[path] = "too_deep"
            else:
                _remove(tree, {id(s) for s in tree.body if _is_bare(s)})
                self.modules[path] = _Module(path, tree, _is_test(path))
        report.python = len(self.modules)
        self.entry_points = list(_entry_points(g.root, g.skipped))
        self.buried: list[tuple[str, set[str]]] = []
        """Every dead symbol so far, with the names it used."""

    def run(self) -> None:
        """Fill the report's dead and dynamic lists."""
        first = True
        while True:
            self._survey()
            dead: list[tuple[Finding, _Def | None]] = []
            dynamic: list[Finding] = []
            for finding, definition in self._candidates():
                lookup = self._reached_by(definition, finding.path)
                if lookup is not None:
                    low = replace(finding, confidence=DYNAMIC_CONFIDENCE, detail=lookup.evidence)
                    dynamic.append(low)
                    continue
                if not first:
                    finding = self._through(finding, definition)
                dead.append((finding, definition))
            self.report.dead += [f for f, _ in dead]
            self.report.dynamic = dynamic
            if not dead:
                return
            self._bury(dead)
            first = False

    def _survey(self) -> None:
        """Read every live module's current tree again."""
        self.defs = {p: _definitions(m) for p, m in self.modules.items()}
        self.classes: dict[str, list[ast.ClassDef]] = defaultdict(list)
        for definition in (d for defs in self.defs.values() for d in defs):
            if isinstance(definition.node, ast.ClassDef):
                self.classes[definition.name].append(definition.node)
        names = set(self.classes)
        self.facts = {p: _Reader(m, self.resolve, names).read() for p, m in self.modules.items()}
        self.lookups = [k for f in self.facts.values() for k in f.lookups] + self.entry_points
        self.escaped = {e for f in self.facts.values() for e in f.escaped}
        self.annotated = {n for f in self.facts.values() for n in f.annotated}
        self.families = self._families()
        self.exported = self._exported()

    def _candidates(self) -> list[tuple[Finding, _Def | None]]:
        """Return what no static use reaches: modules, then definitions not part of another one."""
        found: list[tuple[Finding, _Def | None]] = []
        unused = self._unused_modules()
        for path in unused:
            detail = "no module in the tree imports it"
            found.append((self._finding("module", path, 1, path, "module", detail), None))
        rules: dict[int, tuple[_Def, str, str]] = {}
        for definition, confidence in self._vulture():
            detail = f"no use of the name {definition.name} in the tree"
            rules[id(definition.node)] = (definition, f"vulture:{confidence}", detail)
        for definition in self._private():
            if id(definition.node) not in rules:
                detail = "its own module never uses it and no other module imports or reads it"
                rules[id(definition.node)] = (definition, "private", detail)
        dead_modules = {f.path for f, d in found if self._reached_by(d, f.path) is None}
        for definition, rule, detail in rules.values():
            path = definition.module.path
            if path in dead_modules or any(around in rules for around in definition.inside):
                continue
            line, symbol, kind = definition.node.lineno, definition.symbol, definition.kind
            name, _, confidence = rule.partition(":")
            finding = self._finding(name, path, line, symbol, kind, detail)
            if confidence:
                finding = replace(finding, confidence=int(confidence))
            found.append((finding, definition))
        return found

    def _finding(
        self, rule: str, path: str, line: int, symbol: str, kind: str, detail: str
    ) -> Finding:
        return Finding(path, line, symbol, kind, CONFIDENCE[rule], rule, detail)

    def _vulture(self) -> list[tuple[_Def, int]]:
        """Return the definitions outside test files that vulture finds unused, with its confidence.

        Vulture sees a made-up file name, because it treats any path that looks like a test's as one
        and the directory above the repository is no part of the question.
        """
        scanner = Vulture()
        scanner.noqa_lines = defaultdict(set)
        places: dict[Path, str] = {}
        for number, (path, module) in enumerate(sorted(self.modules.items())):
            directory, stem = ("tests", "test_") if module.test else ("scan", "module_")
            name = Path(f"/{directory}/{stem}{number}.py")
            places[name] = path
            scanner.filename = name
            scanner.code = []
            scanner.visit(module.tree)
        wanted = ("function", "class", "method", "property")
        known = {
            (d.module.path, d.name, line): d
            for defs in self.defs.values()
            for d in defs
            for line in {d.node.lineno, *(x.lineno for x in d.node.decorator_list[:1])}
        }
        found: list[tuple[_Def, int]] = []
        for item in scanner.get_unused_code():
            definition = known.get((places[item.filename], item.name, item.first_lineno))
            if item.typ not in wanted or definition is None or definition.module.test:
                continue
            if item.name not in self.annotated and not self._is_exported(definition):
                found.append((definition, item.confidence))
        return found

    def _private(self) -> list[_Def]:
        """Return the module-private definitions that nothing uses, whatever else shares the name."""
        found: list[_Def] = []
        for path, defs in self.defs.items():
            for d in defs:
                private = d.name.startswith("_") and not d.name.startswith("__")
                if not (private and d.top) or d.module.test or self._is_exported(d):
                    continue
                inside = sum(
                    (isinstance(n, ast.Name) and n.id == d.name)
                    or (isinstance(n, ast.Attribute) and n.attr == d.name)
                    for n in ast.walk(d.node)
                )
                if self.facts[path].loads[d.name] <= inside and not self._used_outside(
                    path, d.name
                ):
                    found.append(d)
        return found

    def _used_outside(self, path: str, name: str) -> bool:
        """Say whether a module other than `path` imports `name` from it or reads it as an attribute."""
        for other, facts in self.facts.items():
            if other == path:
                continue
            places = ((path, name), ("", name))
            if any(p in facts.imported or p in facts.attributes for p in places):
                return True
        return False

    def _unused_modules(self) -> list[str]:
        """Return the modules that no live module imports and that are no entry point."""
        imported = {
            e.target
            for e in self.g.edges
            if e.source in self.modules or e.source in self.report.skipped
            if e.source not in self.whitelists
        }
        for path, facts in self.facts.items():
            imported |= facts.modules - {path}
        found: list[str] = []
        for path, module in self.modules.items():
            name = posixpath.basename(path)
            entry = name in ("__init__.py", "__main__.py", "setup.py") or module.test
            if path not in imported and not entry and not _has_main_block(module.tree):
                found.append(path)
        return found

    # what reaches a definition without a static use

    def _reached_by(self, definition: _Def | None, path: str) -> _Lookup | None:
        """Return the first lookup that may reach a definition, or a module when it is None."""
        if definition is None:
            return self._module_lookup(self.modules[path])
        return (
            self._decorator(definition)
            or self._base(definition)
            or next((k for k in self.lookups if self._reaches(k, definition)), None)
        )

    def _module_lookup(self, module: _Module) -> _Lookup | None:
        for lookup in self.lookups:
            if lookup.path == module.path:
                continue
            dotted = "." + ".".join(module.dotted)
            if lookup.scope == "import":
                start, end = "." + lookup.prefix.lstrip("."), re.escape(lookup.suffix)
                head = re.escape(start) if lookup.prefix.strip(".") else ""
                if re.search(f"{head}.*{end}$", dotted):
                    return lookup
            elif lookup.scope == "string":
                text = lookup.target
                if text.endswith(".py") and f"/{module.path}".endswith(f"/{text}"):
                    return lookup
                parts = re.split(r"[.:]", text)
                shortest = min(2, len(module.dotted))
                for size in range(shortest, len(module.dotted) + 1):
                    if parts[:size] == module.dotted[-size:]:
                        return lookup
        stem = module.dotted[-1]
        for u in self.g.unresolved:
            named = u.source in self.modules and stem in re.findall(r"\w+", u.specifier)
            if named and u.reason in ("ambiguous", "not_found"):
                text = f"import {u.specifier!r} is not resolved ({u.reason})"
                return _Lookup(u.source, u.line, "", text, "import")
        return None

    def _reaches(self, lookup: _Lookup, d: _Def) -> bool:
        path = d.module.path
        if lookup.scope == "import":
            return False
        if lookup.scope == "string":
            parts = re.split(r"[.:]", lookup.target)
            return path != lookup.path and parts[-d.qual.count(".") - 2 :] == [
                d.module.dotted[-1],
                *d.qual.split("."),
            ]
        own = d.node.lineno <= lookup.line <= (d.node.end_lineno or 0) and path == lookup.path
        if own or not lookup.matches(d.name) or not (d.top or d.owner):
            return False
        if lookup.scope == "module":
            package = posixpath.dirname(lookup.target)
            inside = posixpath.basename(lookup.target) == "__init__.py" and (
                path.startswith(f"{package}/") if package else True
            )
            return d.top and (path == lookup.target or inside)
        if lookup.scope == "modules":
            return d.top
        if lookup.scope == "class":
            owner = d.owner.name if d.owner else ""
            return bool(owner) and self.families.get(owner) == self.families.get(lookup.target)
        fixed = bool(lookup.prefix or lookup.suffix)
        return (
            bool(d.owner) or fixed or path in self.escaped or self._imported_at_run_time(d.module)
        )

    def _imported_at_run_time(self, module: _Module) -> bool:
        """Say whether a dynamic import may hand out `module` as a value."""
        lookup = self._module_lookup(module)
        return lookup is not None and lookup.scope == "import"

    def _decorator(self, d: _Def) -> _Lookup | None:
        for decorator in d.node.decorator_list:
            if _last(decorator) not in NEUTRAL_DECORATORS:
                text = f"decorator @{ast.unparse(decorator)[:100]} may register it"
                return _Lookup(d.module.path, decorator.lineno, d.symbol, text, "any")
        return None

    def _base(self, d: _Def) -> _Lookup | None:
        """Return the base class that may register a class or call a method of it by name."""
        is_class = isinstance(d.node, ast.ClassDef)
        start = d.node if isinstance(d.node, ast.ClassDef) else d.owner
        if start is None:
            return None
        seen: set[int] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            hooks = {s.name for s in node.body if isinstance(s, ast.FunctionDef)}
            if is_class and node is not start and "__init_subclass__" in hooks:
                return self._class_lookup(d, start, f"{node.name} has __init_subclass__")
            for base in [*node.bases, *(k.value for k in node.keywords if k.arg == "metaclass")]:
                name = _last(base)
                if is_class:
                    for facts in self.facts.values():
                        for lookup in facts.subclassed:
                            if lookup.target == name:
                                return lookup
                if name in self.classes:
                    stack += self.classes[name]
                elif name not in NEUTRAL_BASES and not self._plain_builtin(name, d):
                    text = f"base class {ast.unparse(base)[:80]} is not in the tree"
                    return self._class_lookup(d, start, text)
        return None

    def _plain_builtin(self, name: str, d: _Def) -> bool:
        """Say whether `name` is a built-in class that knows nothing of the definition's name."""
        base = getattr(builtins, name, None)
        return isinstance(base, type) and not (d.owner is not None and hasattr(base, d.name))

    def _class_lookup(self, d: _Def, node: ast.ClassDef, text: str) -> _Lookup:
        symbol = f"{d.module.path}::{node.name}"
        return _Lookup(d.module.path, node.lineno, symbol, text, "class", node.name)

    def _families(self) -> dict[str, str]:
        """Return a class name's family: the classes it is tied to by inheritance share one."""
        parent = {name: name for name in self.classes}

        def find(name: str) -> str:
            while parent[name] != name:
                name = parent[name]
            return name

        for name, nodes in self.classes.items():
            for base in (_last(b) for node in nodes for b in node.bases):
                if base in parent:
                    parent[find(name)] = find(base)
        return {name: find(name) for name in parent}

    def _exported(self) -> set[tuple[str, str]]:
        """Return `(path, name)` for every name an `__init__.py` re-exports, followed to its file.

        The name `*` stands for every public name of the file.
        """
        found: set[tuple[str, str]] = set()
        todo: list[tuple[str, str]] = []
        for path, facts in self.facts.items():
            if posixpath.basename(path) == "__init__.py":
                todo += [*facts.bound.values(), *((star, "*") for star in facts.stars)]
        while todo:
            item = todo.pop()
            if item in found:
                continue
            found.add(item)
            facts = self.facts.get(item[0])
            if facts is None:
                continue
            todo += [(star, item[1]) for star in facts.stars]
            if item[1] == "*":
                todo += facts.bound.values()
            elif item[1] in facts.bound:
                todo.append(facts.bound[item[1]])
        return found

    def _is_exported(self, d: _Def) -> bool:
        if not d.top:
            return False
        path = d.module.path
        public = not d.name.startswith("_")
        return (path, d.name) in self.exported or (public and (path, "*") in self.exported)

    # taking the dead out

    def _through(self, finding: Finding, definition: _Def | None) -> Finding:
        """Return a finding of a later round: the dead code taken out before it was its only use."""
        key = definition.name if definition else finding.path
        users = sorted({symbol for symbol, names in self.buried if key in names})[:3]
        detail = "only dead code uses it" + (f": {', '.join(users)}" if users else "")
        confidence = min(finding.confidence, CONFIDENCE["through_dead"])
        return replace(finding, rule="through_dead", confidence=confidence, detail=detail)

    def _bury(self, dead: Iterable[tuple[Finding, _Def | None]]) -> None:
        """Take dead definitions and modules out of what the next round reads."""
        doomed: dict[str, set[int]] = defaultdict(set)
        for finding, definition in dead:
            tree: ast.AST = definition.node if definition else self.modules[finding.path].tree
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            if definition is None:
                names |= {e.target for e in self.g.edges if e.source == finding.path}
            else:
                doomed[finding.path].add(id(definition.node))
            self.buried.append((finding.symbol, names))
        for finding, definition in dead:
            if definition is None:
                del self.modules[finding.path]
        for path, ids in doomed.items():
            if path in self.modules:
                _remove(self.modules[path].tree, ids)


# --- TypeScript and JavaScript ------------------------------------------------------------------

_EXPORT = re.compile(
    r"(?:^|(?<=[;}]))[ \t]*export\s+(?:declare\s+)?(?:"
    r"(?P<default>default)\b"
    r"|(?:abstract\s+)?(?:async\s+)?(?:const\s+enum|function\s*\*?|class|const|let|var|interface"
    r"|type|enum|namespace)\s+(?P<name>[A-Za-z_$][\w$]*)"
    r"|(?:type\s*)?\{(?P<list>[^{}]*)\}(?!\s*from\b))",
    re.MULTILINE,
)
_FROM = re.compile(r"(?:import|export)\b\s*(?P<clause>.*?)\s*\bfrom\s*['\"]", re.DOTALL)
_GLOB = re.compile(r"\b(?:import\.meta\.glob\w*|require\.context)\s*(?:<[^<>()]*>)?\(")
_LEADING = re.compile(r"\s*(['\"`])([^'\"`$*]*)")
_NOT_REPORTED = re.compile(
    r"(?:^|/)(?:index\.[^/]+|[^/]+\.(?:d|config|test|spec|stories)\.[^/]+)\Z"
    r"|(?:^|/)(?:tests?|__tests__|__mocks__)/"
    r"|\.d\.[mc]?ts\Z"
)
_WHOLE = "*"


def _clause(clause: str) -> tuple[set[str], dict[str, str]]:
    """Return the names an import or re-export clause takes, and the name each local name stands for.

    `default` is the default export and `*` the whole module.
    """
    taken: set[str] = set()
    local: dict[str, str] = {}
    clause = re.sub(r"^type\b(?!\s*,)\s*(?=[\w${*])", "", clause)
    braces = re.search(r"\{([^{}]*)\}", clause)
    for item in braces.group(1).split(",") if braces else ():
        words = re.sub(r"^type\s+(?!as\b)", "", item.strip()).split()
        if words:
            taken.add(words[0])
            local[words[-1]] = words[0]
    rest = re.sub(r"\{[^{}]*\}", "", clause)
    for item in rest.split(","):
        words = item.split()
        if not words:
            continue
        name = _WHOLE if words[0].startswith("*") else "default"
        taken.add(name)
        local[words[-1]] = name
    return taken, local


@dataclass(frozen=True)
class _ScriptLookup:
    """An `import()` or a glob of files whose path is not a literal."""

    path: str
    line: int
    text: str
    prefix: str
    """The fixed start of the path of the files it may load, empty when it may load any."""


class _Scripts:
    """Reports the exports of TypeScript and JavaScript files that no other file imports."""

    def __init__(self, g: Graph, report: Report) -> None:
        from tools import deps

        self.g = g
        self.report = report
        self.deps = deps
        scripts = {p for p, kind in g.modules.items() if kind in ("typescript", "javascript")}
        self.paths = sorted(scripts - set(g.skipped))
        known = set(g.modules) | {p for p, reason in g.skipped.items() if reason != "symlink"}
        for name in ("tsconfig.json", "jsconfig.json"):
            if (g.root / name).is_file() and not (g.root / name).is_symlink():
                known.add(name)
        self.resolver = deps._ScriptResolver(g.root, known)
        self.used: set[tuple[str, str]] = set()
        """`(path, name)` of every export imported; the name `*` stands for all of them."""
        self.anywhere: set[str] = set()
        """Names imported through an unresolved path alias: they may come from any file."""
        self.by_stem: set[tuple[str, str]] = set()
        """`(last path component, name)` of alias imports of a default export or a whole module."""
        self.lookups: list[_ScriptLookup] = []
        self.exports: dict[tuple[str, str], int] = {}
        report.scripts = len(self.paths)

    def run(self) -> None:
        """Fill the report's exports and add to its dynamic list."""
        for path in self.paths:
            text = (self.g.root / path).read_bytes().decode("utf-8", errors="replace")
            self._read(path, self.deps._blank_comments(text))
        for (path, name), line in self.exports.items():
            if self._is_used(path, name):
                continue
            symbol = f"{path}::{name}"
            detail = "no other file in the tree imports it"
            finding = Finding(path, line, symbol, "export", EXPORT_CONFIDENCE, "export", detail)
            lookup = next((k for k in self.lookups if path.startswith(k.prefix)), None)
            if lookup is None:
                self.report.exports.append(finding)
            else:
                detail = f"{lookup.path}:{lookup.line}: {lookup.text}"
                low = replace(finding, confidence=DYNAMIC_CONFIDENCE, detail=detail)
                self.report.dynamic.append(low)

    def _read(self, path: str, text: str) -> None:
        imported: dict[str, str] = {}
        for match in self.deps._STATIC.finditer(text):
            clause = _FROM.match(match.group(0))
            taken, local = _clause(clause.group("clause")) if clause else (set(), {})
            if match.group(0).startswith("import"):
                imported.update(local)
            elif _WHOLE not in taken and "default" in taken and "{" not in match.group(0):
                taken = {_WHOLE}
            self._use(path, match.group(2), taken)
        for match in self.deps._CALL.finditer(text):
            if match.group(3) is not None:
                self._use(path, match.group(3), {_WHOLE})
            elif match.group(1) == "import":
                self._lookup(path, text, match)
        for match in _GLOB.finditer(text):
            self._lookup(path, text, match)
        if not _NOT_REPORTED.search(path):
            for name, offset in self._exported(text, imported):
                self.exports.setdefault((path, name), text.count("\n", 0, offset) + 1)

    def _exported(self, text: str, imported: dict[str, str]) -> Iterator[tuple[str, int]]:
        """Yield the names a file exports itself, with their offsets; a re-export is not one."""
        for match in _EXPORT.finditer(text):
            if match.group("default"):
                yield "default", match.start("default")
            elif match.group("name"):
                yield match.group("name"), match.start("name")
            else:
                for item in re.finditer(r"[^,]+", match.group("list") or ""):
                    words = re.sub(r"^type\s+(?!as\b)", "", item.group(0).strip()).split()
                    if words and words[0] not in imported:
                        start = match.start("list") + item.start()
                        blank = len(item.group(0)) - len(item.group(0).lstrip())
                        yield words[-1], start + blank

    def _use(self, path: str, specifier: str, names: set[str]) -> None:
        outcome = self.resolver.resolve(path, self.deps._Import(0, specifier))
        if isinstance(outcome, str):
            if outcome != path:
                self.used |= {(outcome, name) for name in names}
        elif outcome == ("unresolved", "alias") or outcome == ("unresolved", "absolute"):
            stem = posixpath.basename(specifier.rstrip("/")).split(".")[0]
            for name in names:
                if name in ("default", _WHOLE):
                    self.by_stem.add((stem, name))
                else:
                    self.anywhere.add(name)

    def _lookup(self, path: str, text: str, match: re.Match[str]) -> None:
        rest = text[match.end() :]
        literal = _LEADING.match(rest)
        fixed = literal.group(2) if literal else ""
        prefix = ""
        if fixed.startswith(("./", "../")):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), fixed))
            prefix = joined + "/" if fixed.endswith("/") else joined
            prefix = "" if prefix.startswith((".", "..")) and joined == "." else prefix
        shown = " ".join((match.group(0) + rest.split("\n", 1)[0]).split())[:120]
        line = text.count("\n", 0, match.start()) + 1
        self.lookups.append(_ScriptLookup(path, line, shown, prefix))

    def _is_used(self, path: str, name: str) -> bool:
        if (path, name) in self.used or (path, _WHOLE) in self.used or name in self.anywhere:
            return True
        stem = posixpath.basename(path).split(".")[0]
        return (stem, name) in self.by_stem or (stem, _WHOLE) in self.by_stem


DEAD_CODE = ToolSpec(
    name="deps.dead_code",
    description=(
        "Report the code of the repository that nothing appears to use. Each finding is one line, "
        "path:line: symbol (kind, confidence, rule): reason, where symbol is path::Qualified.name "
        "or the path alone for a whole module. The whole tree is always read, statically: nothing "
        "is imported or run and no setting in the repository changes the result. Python "
        "functions, classes, methods, properties and modules are covered by vulture plus rules for "
        "private definitions, modules nothing imports and code only dead code uses; variables, "
        "imports and unreachable statements are not reported. A use in a test is a use. A symbol "
        "that a dynamic lookup may reach is in a separate list with that lookup's path and line, "
        "and must not be taken as dead. Names are matched without types, so a dead method that "
        "shares its name with a live one is missed, and a caller outside the tree is not seen. "
        "TypeScript and JavaScript get only a list of exports no other file imports, at a lower "
        "confidence. A finding is a lead to verify against the code, not a result."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "List only findings in this file or directory, relative to the repository "
                    "root. The whole tree is still read."
                ),
            },
        },
        "additionalProperties": False,
    },
    impl=dead_code,
)
"""The declaration `tools.deps` lists in its `TOOLS`."""
