"""`deps.call_graph`: who calls a Python symbol and what it calls, read from the source alone.

Every Python file `build_graph` reads is parsed with `ast`; nothing is imported or run. A symbol is
`path::Qualified.name`, such as `app/core.py::Ledger.post` or `app/core.py::outer.inner`; a path
alone is the module, meaning its top-level code.

A call is an edge when reading the source ties it to one definition: a name defined or imported in
the module, a name on an imported module, a method through `self`, `cls` or `super()`, a method of
a class or of an object the same scope builds by calling that class, or a name whose only
assignment in its scope is one of those. Calling a class is an edge to the class and, when the
class or a base in the repository defines one, to its constructor. Any other call is unresolved,
with a reason, and never guessed:

- `getattr`: the function comes from `getattr` with a computed name.
- `container`: the function was taken out of a container, by subscript or by iterating over it.
- `parameter`: the function was passed in as a parameter.
- `unknown_type`: a method of an object whose type the source does not fix.
- `call_result`: the function is what another call returned.
- `variable`: the function is a variable whose value the source does not fix.
- `ambiguous`: the name has more than one definition it may stand for.
- `inherited`: the method may come from a base class the tool does not read.
- `import`: the name comes from an import that names no module the tool read.
- `star_import`: the name may come from a `from module import *`.
- `not_defined`: the module, class or scope defines no such name.
- `expression`: the called expression is of some other kind, such as a lambda or a conditional.

A call of a builtin, the standard library or an installed package is external and only counted.
A definition used as a value, not called, is a reference and is listed apart from the calls.

The helpers of `tools.deps` are imported inside `build_call_graph`, because `tools.deps` imports
this module to declare the tool.
"""

import ast
import builtins
import difflib
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tools._common import (
    DEFAULT_MAX_BYTES,
    ToolInputError,
    ToolSpec,
    resolve_inside,
    truncate,
)

if TYPE_CHECKING:
    from tools.deps import _PythonResolver

DIRECTIONS = ("both", "callees", "callers")
DEFAULT_DEPTH = 3
MAX_DEPTH = 10
_MAX_EXPRESSION = 100
_MAX_NEAR = 10
_BUILTINS = frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__path__"}


# --- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class CallEdge:
    """One call tied to one definition; `path` and `line` are where the call is written."""

    path: str
    line: int
    caller: str
    callee: str


@dataclass(frozen=True, order=True)
class UnresolvedCall:
    """One call not tied to a definition, with its source text and the reason.

    `name` is the attribute or plain name that is called, or an empty string when there is none.
    """

    path: str
    line: int
    caller: str
    expression: str
    reason: str
    name: str = ""


@dataclass(frozen=True, order=True)
class Reference:
    """One use of the definition `target` as a value, inside the symbol `holder`."""

    path: str
    line: int
    holder: str
    target: str


@dataclass
class CallGraph:
    """The calls of every Python module that could be read.

    `definitions` maps each symbol to the line it is defined on, a module to 0. `constructors` maps
    a class to the `__init__` or `__new__` that calling it runs, when that is in the repository.
    `external` counts the external calls of each caller, and `skipped` gives each file that was not
    read with the reason.
    """

    root: Path
    definitions: dict[str, int] = field(default_factory=dict)
    constructors: dict[str, str] = field(default_factory=dict)
    edges: set[CallEdge] = field(default_factory=set)
    unresolved: set[UnresolvedCall] = field(default_factory=set)
    references: set[Reference] = field(default_factory=set)
    external: Counter[str] = field(default_factory=Counter)
    skipped: dict[str, str] = field(default_factory=dict)


# --- what an expression stands for --------------------------------------------------------------


@dataclass(frozen=True)
class _Symbol:
    """A function, method or class defined in the repository."""

    path: str
    qual: str
    is_class: bool

    @property
    def name(self) -> str:
        return f"{self.path}::{self.qual}"


@dataclass(frozen=True)
class _Module:
    """A module of the repository, or a directory on the way to one, imported as `dotted`."""

    path: str | None
    source: str
    dotted: str


@dataclass(frozen=True)
class _Instance:
    """An object of the class `cls`, or through `super()` of its bases only."""

    cls: _Symbol
    bases_only: bool = False


@dataclass(frozen=True)
class _External:
    """A builtin, or something from outside the repository."""

    name: str


@dataclass(frozen=True)
class _Unknown:
    """Something the source does not fix, and why."""

    reason: str


type _Value = _Symbol | _Module | _Instance | _External | _Unknown

_CYCLE = _Unknown("variable")


# --- bindings and scopes ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Import:
    """`import specifier` when `name` is empty, otherwise `from specifier import name`.

    `whole` says an `import a.b` binds the module itself, as `import a.b as c` does.
    """

    line: int
    specifier: str
    name: str = ""
    whole: bool = False


@dataclass(frozen=True, eq=False)
class _Assigned:
    """A name whose value is the expression `value`, read in `scope`."""

    value: ast.expr
    scope: _Scope


type _Binding = _Symbol | _Instance | _Unknown | _Import | _Assigned


@dataclass(eq=False)
class _Scope:
    """One scope: a module, a class, a function, or an inner one (a lambda or a comprehension).

    `owner` is the symbol the calls of the scope belong to, and `prefix` the qualified name that
    definitions inside it start from. Code in a class body belongs to what encloses the class.
    """

    kind: str
    path: str
    owner: str
    prefix: str
    parent: _Scope | None = None
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)
    stars: list[_Import] = field(default_factory=list)
    bases: list[ast.expr] = field(default_factory=list)
    cls: _Symbol | None = None

    @property
    def module(self) -> _Scope:
        """The module scope this scope is in."""
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    @property
    def method_class(self) -> _Symbol | None:
        """The class of the method this scope is in, or None outside a method."""
        scope: _Scope | None = self
        while scope is not None:
            if scope.kind == "function" and scope.parent and scope.parent.kind == "class":
                return scope.parent.cls
            scope = scope.parent
        return None


class _Walker(ast.NodeVisitor):
    """Walks one module scope by scope. Annotations are not walked: they are types, not values."""

    def __init__(self, scope: _Scope) -> None:
        self.scope = scope

    def child(self, node: ast.AST) -> _Scope:
        """Return the scope that `node`, read in the current scope, opens."""
        raise NotImplementedError

    def decorators(self, nodes: list[ast.expr]) -> None:
        """Handle the decorators of a definition of the current scope."""
        self.all(nodes)

    def all(self, nodes: Iterable[ast.AST | None]) -> None:
        """Visit each node that is not None."""
        for node in nodes:
            if node is not None:
                self.visit(node)

    def inside(self, scope: _Scope, nodes: Iterable[ast.AST | None]) -> None:
        """Visit `nodes` with `scope` as the current scope."""
        outer, self.scope = self.scope, scope
        try:
            self.all(nodes)
        finally:
            self.scope = outer

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.decorators(node.decorator_list)
        self.all([*node.args.defaults, *node.args.kw_defaults])
        self.inside(self.child(node), node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.all([*node.args.defaults, *node.args.kw_defaults])
        self.inside(self.child(node), [node.body])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.decorators(node.decorator_list)
        self.all([*node.bases, *(k.value for k in node.keywords)])
        self.inside(self.child(node), node.body)

    def _comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        self.visit(node.generators[0].iter)
        results = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        inner: list[ast.AST | None] = []
        for n, gen in enumerate(node.generators):
            inner += [gen.target, *([gen.iter] if n else []), *gen.ifs]
        self.inside(self.child(node), [*inner, *results])

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.all([node.target, node.value])

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.visit(node.name)


class _Collector(_Walker):
    """First walk of a module: its scopes, what each name in them is bound to, its definitions."""

    def __init__(self, path: str) -> None:
        super().__init__(_Scope("module", path, path, ""))
        self.module = self.scope
        self.path = path
        self.scopes: dict[ast.AST, _Scope] = {}
        self.classes: dict[str, _Scope] = {}
        self.definitions: dict[str, int] = {}
        self.stores: list[tuple[_Scope, ast.Attribute]] = []

    def bind(self, name: str, binding: _Binding, scope: _Scope | None = None) -> None:
        """Add `binding` to `name` in `scope`, or where `global` or `nonlocal` puts the name."""
        scope = scope or self.scope
        if name in scope.globals:
            scope = scope.module
        elif name in scope.nonlocals:
            outer = scope.parent
            while outer and not (outer.kind == "function" and name in outer.bindings):
                outer = outer.parent
            scope = outer or scope
        scope.bindings.setdefault(name, []).append(binding)

    def child(self, node: ast.AST) -> _Scope:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            scope = self._definition(node)
        else:
            scope = _Scope("inner", self.path, self.scope.owner, self.scope.prefix, self.scope)
            if isinstance(node, ast.Lambda):
                self._parameters(scope, node.args)
        self.scopes[node] = scope
        return scope

    def _definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> _Scope:
        prefix = self.scope.prefix
        qual = f"{prefix}.{node.name}" if prefix else node.name
        is_class = isinstance(node, ast.ClassDef)
        symbol = _Symbol(self.path, qual, is_class)
        self.bind(node.name, symbol)
        self.definitions.setdefault(symbol.name, node.lineno)
        if isinstance(node, ast.ClassDef):
            scope = self.classes.get(qual)
            if scope is None:
                owner = self.scope.owner
                scope = _Scope("class", self.path, owner, qual, self.scope, cls=symbol)
                self.classes[qual] = scope
            scope.bases += node.bases
        else:
            scope = _Scope("function", self.path, symbol.name, qual, self.scope)
            self._parameters(scope, node.args)
            self._receiver(scope, node)
        for parameter in node.type_params:
            if isinstance(parameter, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple):
                scope.bindings.setdefault(parameter.name, []).append(_Unknown("variable"))
        return scope

    def _parameters(self, scope: _Scope, args: ast.arguments) -> None:
        listed = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        for arg in [*listed, *(a for a in (args.vararg, args.kwarg) if a)]:
            scope.bindings[arg.arg] = [_Unknown("parameter")]

    def _receiver(self, scope: _Scope, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Bind the first parameter of a method to its class or to an object of it."""
        cls = self.scope.cls
        first = [*node.args.posonlyargs, *node.args.args][:1]
        names = {d.id for d in node.decorator_list if isinstance(d, ast.Name)}
        if cls is None or not first or "staticmethod" in names:
            return
        implicit = ("__new__", "__init_subclass__", "__class_getitem__")
        on_class = "classmethod" in names or node.name in implicit
        scope.bindings[first[0].arg] = [cls if on_class else _Instance(cls)]

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.partition(".")[0]
            self.bind(name, _Import(node.lineno, alias.name, whole=alias.asname is not None))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = "." * node.level + (node.module or "")
        for alias in node.names:
            imp = _Import(node.lineno, base, alias.name)
            if alias.name == "*":
                self.scope.stars.append(imp)
            else:
                self.bind(alias.asname or alias.name, imp)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.nonlocals.update(node.names)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._assign(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._assign(node.target, node.value)

    def _assign(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self.bind(target.id, _Assigned(value, self.scope))
        else:
            self.visit(target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        scope = self.scope
        while scope.kind == "inner" and scope.parent:
            scope = scope.parent
        self.bind(node.target.id, _Assigned(node.value, self.scope), scope)

    def _loop(self, node: ast.For | ast.AsyncFor) -> None:
        self._taken(node.target)
        self.all([node.iter, *node.body, *node.orelse])

    def visit_For(self, node: ast.For) -> None:
        self._loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._loop(node)

    def _comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        super()._comprehension(node)
        scope = self.scopes[node]
        for name in list(scope.bindings):
            scope.bindings[name] = [_Unknown("container")]

    def _taken(self, target: ast.expr) -> None:
        """Bind the names of a loop target, whose values come out of a container."""
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    self.bind(node.id, _Unknown("container"))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                self.stores.append((self.scope, node))

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            self.bind(node.id, _Unknown("variable"))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not isinstance(node.ctx, ast.Load):
            self.stores.append((self.scope, node))
        self.visit(node.value)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bind(node.name, _Unknown("variable"))
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        self._captured(node, node.name)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        self._captured(node, node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        self._captured(node, node.rest)

    def _captured(self, node: ast.AST, name: str | None) -> None:
        if name:
            self.bind(name, _Unknown("variable"))
        self.generic_visit(node)


# --- resolution ---------------------------------------------------------------------------------


class _Names:
    """Ties names and expressions of the collected modules to what they stand for."""

    def __init__(self, modules: dict[str, _Collector], resolver: _PythonResolver) -> None:
        self.modules = modules
        self.resolver = resolver
        self._busy: set[tuple[str, str]] = set()
        self._known: dict[tuple[str, str], _Value] = {}
        self._ready = False
        stored = [
            (scope, node, self.value(node.value, scope))
            for collector in modules.values()
            for scope, node in collector.stores
        ]
        for scope, node, owner in stored:
            self._override(scope, node, owner)
        self._ready = True

    def _override(self, scope: _Scope, node: ast.Attribute, owner: _Value) -> None:
        """Note that `node` assigns an attribute of `owner`, so its name has no one value.

        A definition keeps its name when the assignment is outside its class, since replacing a
        function from outside is mostly a test double and would hide every call of it.
        """
        if isinstance(owner, _Module) and owner.path in self.modules:
            bindings = self.modules[owner.path].module.bindings
        elif isinstance(owner, _Instance) or (isinstance(owner, _Symbol) and owner.is_class):
            cls = owner.cls if isinstance(owner, _Instance) else owner
            bindings = self.modules[cls.path].classes[cls.qual].bindings
        else:
            return
        known = bindings.get(node.attr, [])
        inside = isinstance(owner, _Instance) and owner.cls == scope.method_class
        if inside or not any(isinstance(b, _Symbol) for b in known):
            bindings.setdefault(node.attr, []).append(_Unknown("variable"))

    # expressions

    def value(self, node: ast.expr, scope: _Scope) -> _Value:
        """Return what the expression `node`, read in `scope`, stands for."""
        match node:
            case ast.Name(id=name):
                return self.name(name, scope)
            case ast.Attribute(value=owner, attr=attr):
                return self.attribute(self.value(owner, scope), attr)
            case ast.Call():
                return self._result(node, scope)
            case ast.Subscript():
                return _Unknown("container")
            case ast.Constant() | ast.JoinedStr():
                return _External("literal")
            case ast.List() | ast.Dict() | ast.Set() | ast.Tuple():
                return _External("literal")
            case ast.ListComp() | ast.DictComp() | ast.SetComp() | ast.GeneratorExp():
                return _External("literal")
            case ast.NamedExpr(value=inner) | ast.Await(value=inner):
                found = self.value(inner, scope)
                awaited = isinstance(node, ast.Await)
                return _Unknown("call_result") if awaited else found
        return _Unknown("expression")

    def _result(self, node: ast.Call, scope: _Scope) -> _Value:
        """Return what a call returns, where reading it can say so."""
        called = self.value(node.func, scope)
        if isinstance(called, _Symbol) and called.is_class:
            return _Instance(called)
        if called == _External("super") and not node.args:
            cls = scope.method_class
            return _Instance(cls, bases_only=True) if cls else _Unknown("expression")
        if called == _External("getattr") and len(node.args) >= 2:
            name = node.args[1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                found = self.attribute(self.value(node.args[0], scope), name.value)
                if not isinstance(found, _Unknown):
                    return found
            return _Unknown("getattr")
        return _Unknown("call_result")

    # names

    def name(self, name: str, scope: _Scope) -> _Value:
        """Return what `name` stands for in `scope`, by Python's scope rules."""
        at: _Scope | None = scope.module if name in scope.globals else scope
        while at is not None:
            visible = at.kind != "class" or at is scope
            if visible and name in at.bindings:
                return self._bound(at, name)
            if at.parent is None or (at is not scope and name in at.globals):
                at = None if at.parent is None else at.module
            else:
                at = at.parent
        return self._unbound(name, scope)

    def _unbound(self, name: str, scope: _Scope) -> _Value:
        stars = scope.module.stars
        while scope.parent is not None:
            stars, scope = [*stars, *scope.stars], scope.parent
        if name in _BUILTINS and not any(self._star_binds(s, name, scope.path) for s in stars):
            return _External(name)
        return _Unknown("star_import" if stars else "not_defined")

    def _star_binds(self, imp: _Import, name: str, source: str) -> bool:
        target = self._target(source, imp.line, imp.specifier)
        collector = self.modules.get(target) if isinstance(target, str) else None
        return collector is not None and name in collector.module.bindings

    def _bound(self, scope: _Scope, name: str) -> _Value:
        """Return the one value every binding of `name` in `scope` agrees on, or why none is."""
        key = (f"{id(scope)}", name)
        if key in self._busy:
            return _CYCLE
        if key in self._known:
            return self._known[key]
        top = not self._busy
        self._busy.add(key)
        try:
            found = {self._binding(b, scope.path) for b in scope.bindings[name]}
        finally:
            self._busy.discard(key)
        value = found.pop() if len(found) == 1 else _Unknown("ambiguous")
        if top and self._ready:
            # An answer found while others are being worked out may hold a cut cycle.
            self._known[key] = value
        return value

    def _binding(self, binding: _Binding, source: str) -> _Value:
        if isinstance(binding, _Import):
            return self._imported(binding, source)
        if isinstance(binding, _Assigned):
            return self.value(binding.value, binding.scope)
        return binding

    # imports

    def _target(self, source: str, line: int, specifier: str, name: str = "") -> str | _Value:
        from tools import deps

        outcome = self.resolver.resolve(source, deps._Import(line, specifier, name=name))
        if isinstance(outcome, str):
            return outcome
        shown = f"{specifier}.{name}" if name else specifier
        return _External(shown) if outcome[0] == "external" else _Unknown("import")

    def _imported(self, imp: _Import, source: str) -> _Value:
        target = self._target(source, imp.line, imp.specifier, imp.name)
        if not isinstance(target, str):
            return target
        if not imp.name:
            if imp.whole:
                return _Module(target, source, imp.specifier)
            first = imp.specifier.partition(".")[0]
            top = self._target(source, imp.line, first)
            return _Module(top if isinstance(top, str) else None, source, first)
        base = self._target(source, imp.line, imp.specifier)
        if target == base:
            return self._member(target, imp.name)
        if isinstance(base, str) and base in self.modules:
            # A name the package binds wins over the submodule of that name, unless the binding
            # is this import or one that leads back to it.
            scope = self.modules[base].module
            if imp.name in scope.bindings and (f"{id(scope)}", imp.name) not in self._busy:
                found = self._bound(scope, imp.name)
                if found != _CYCLE:
                    return found
        dotted = imp.specifier + ("" if imp.specifier.endswith(".") else ".") + imp.name
        return _Module(target, source, dotted)

    def _member(self, path: str, name: str) -> _Value:
        """Return what `name` stands for at the top level of the module `path`."""
        collector = self.modules.get(path)
        if collector is None:
            return _Unknown("import")
        if name in collector.module.bindings:
            return self._bound(collector.module, name)
        return _Unknown("star_import" if collector.module.stars else "not_defined")

    # attributes

    def attribute(self, owner: _Value, attr: str) -> _Value:
        """Return what `.attr` of `owner` stands for."""
        match owner:
            case _External(name=name):
                return _External(f"{name}.{attr}")
            case _Module(path=path, source=source, dotted=dotted):
                found = self._member(path, attr) if path else _Unknown("not_defined")
                if found == _Unknown("not_defined"):
                    inner = dotted + ("" if dotted.endswith(".") else ".") + attr
                    target = self._target(source, 0, inner)
                    if isinstance(target, str):
                        return _Module(target, source, inner)
                return found
            case _Instance(cls=cls, bases_only=bases_only):
                return self._inherited(cls, attr) if bases_only else self._own(cls, attr)
            case _Symbol(is_class=True):
                return self._own(owner, attr)
            case _Unknown(reason="import" | "star_import"):
                return owner
        return _Unknown("unknown_type")

    def _own(self, cls: _Symbol, attr: str) -> _Value:
        """Return the attribute `attr` of the class `cls` or of the bases it inherits it from."""
        scope = self.modules[cls.path].classes[cls.qual]
        return self._bound(scope, attr) if attr in scope.bindings else self._inherited(cls, attr)

    def _inherited(self, cls: _Symbol, attr: str) -> _Value:
        """Return `attr` from the bases of `cls`, when they leave one answer."""
        key = (cls.name, f"bases.{attr}")
        if key in self._busy:
            return _Unknown("inherited")
        self._busy.add(key)
        hits: list[_Value] = []
        unread = False
        try:
            scope = self.modules[cls.path].classes[cls.qual]
            for node in scope.bases:
                base = self.value(node, scope.parent or scope)
                if base == _External("object"):
                    continue
                readable = isinstance(base, _Symbol) and base.is_class
                found = self._own(base, attr) if readable else _Unknown("inherited")
                if found == _Unknown("inherited"):
                    if not hits:
                        return found
                    unread = True
                elif found != _Unknown("not_defined") and found not in hits:
                    hits.append(found)
        finally:
            self._busy.discard(key)
        if len(hits) == 1:
            return hits[0]
        if hits:
            return _Unknown("ambiguous")
        return _Unknown("inherited" if unread else "not_defined")

    def constructor(self, cls: _Symbol) -> _Symbol | None:
        """Return the `__init__` or `__new__` in the repository that calling `cls` runs, if any."""
        for attr in ("__init__", "__new__"):
            found = self._own(cls, attr)
            if isinstance(found, _Symbol) and not found.is_class:
                return found
        return None


# --- reading the calls --------------------------------------------------------------------------


class _Reader(_Walker):
    """Second walk of a module: its calls and references, added to the graph."""

    def __init__(self, collector: _Collector, names: _Names, g: CallGraph) -> None:
        super().__init__(collector.module)
        self.collector = collector
        self.names = names
        self.g = g

    def child(self, node: ast.AST) -> _Scope:
        return self.collector.scopes[node]

    def decorators(self, nodes: list[ast.expr]) -> None:
        """Add the call each decorator is: `@f` calls `f`, and `@f(x)` also calls what `f(x)` gave."""
        for node in nodes:
            if isinstance(node, ast.Call):
                self.visit(node)
                if isinstance(self.names.value(node.func, self.scope), _External):
                    self.g.external[self.scope.owner] += 1
                else:
                    self._call(node, self.names.value(node, self.scope), node)
            elif isinstance(node, ast.Attribute) and node.attr in ("setter", "getter", "deleter"):
                self.g.external[self.scope.owner] += 1
            else:
                value = self.names.value(node, self.scope)
                self._call(node, value, node)
                self._callee(node, value)

    def visit_Call(self, node: ast.Call) -> None:
        value = self.names.value(node.func, self.scope)
        self._call(node.func, value, node)
        self._callee(node.func, value)
        self.all([*node.args, *(k.value for k in node.keywords)])
        if value == _External("getattr"):
            self._refer(node, self.names.value(node, self.scope))

    def _call(self, func: ast.expr, value: _Value, node: ast.expr) -> None:
        """Add the call `node` of `func`, which stands for `value`."""
        path, owner, line = self.scope.path, self.scope.owner, func.end_lineno or func.lineno
        if isinstance(value, _Instance):
            value = self.names.attribute(value, "__call__")
        if isinstance(value, _External):
            self.g.external[owner] += 1
        elif isinstance(value, _Symbol):
            self.g.edges.add(CallEdge(path, line, owner, value.name))
            made = self.names.constructor(value) if value.is_class else None
            if made is not None:
                self.g.edges.add(CallEdge(path, line, owner, made.name))
        else:
            reason = value.reason if isinstance(value, _Unknown) else "expression"
            name = getattr(func, "attr", getattr(func, "id", ""))
            shown = " ".join(ast.unparse(node).split())
            if len(shown) > _MAX_EXPRESSION:
                shown = shown[: _MAX_EXPRESSION - 3] + "..."
            self.g.unresolved.add(UnresolvedCall(path, line, owner, shown, reason, name))

    def _callee(self, func: ast.expr, value: _Value) -> None:
        """Walk the parts of a called expression that are not the name that is called."""
        if isinstance(func, ast.Name):
            return
        if isinstance(func, ast.Attribute):
            if isinstance(value, _Symbol) and _is_chain(func.value):
                return
            self.visit(func.value)
        else:
            self.visit(func)

    def _refer(self, node: ast.expr, value: _Value) -> bool:
        if not isinstance(value, _Symbol):
            return False
        line = node.end_lineno or node.lineno
        self.g.references.add(Reference(self.scope.path, line, self.scope.owner, value.name))
        return True

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._refer(node, self.names.name(node.id, self.scope))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        loaded = isinstance(node.ctx, ast.Load)
        referred = loaded and self._refer(node, self.names.value(node, self.scope))
        if not (referred and _is_chain(node.value)):
            self.visit(node.value)


def _is_chain(node: ast.expr) -> bool:
    """Say whether `node` is a plain dotted name."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def build_call_graph(repo_path: str | Path) -> CallGraph:
    """Read every Python file of the work tree at `repo_path` and return the calls they make.

    The files are the Python modules `tools.deps.build_graph` reads, with its limits, and the
    errors are those it raises. A file that does not parse, or is nested too deeply to walk, is
    listed under `skipped` and the rest is still read. Nothing is imported, run or written.
    """
    from tools import deps

    g = deps.build_graph(repo_path)
    result = CallGraph(g.root)
    result.skipped = {p: r for p, r in g.skipped.items() if p.endswith(deps.PY_SUFFIXES)}
    paths = {p for p in g.modules if p.endswith(deps.PY_SUFFIXES)} | {
        p for p, reason in result.skipped.items() if reason != "symlink"
    }
    modules: dict[str, _Collector] = {}
    trees: dict[str, ast.Module] = {}
    for path in sorted(paths - set(result.skipped)):
        collector = _Collector(path)
        try:
            tree = ast.parse((g.root / path).read_bytes())
            collector.generic_visit(tree)
        except RecursionError:
            result.skipped[path] = "too_deep"
            continue
        modules[path], trees[path] = collector, tree
        result.definitions[path] = 0
        result.definitions.update(collector.definitions)
    names = _Names(modules, deps._PythonResolver(paths))
    for path, collector in modules.items():
        _Reader(collector, names, result).generic_visit(trees[path])
        for qual in collector.classes:
            cls = _Symbol(path, qual, is_class=True)
            made = names.constructor(cls)
            if made is not None:
                result.constructors[cls.name] = made.name
    return result


# --- the query ----------------------------------------------------------------------------------


def check_symbol(g: CallGraph, symbol: object) -> str:
    """Return `symbol` as a key of `g.definitions`, its path relative to the root.

    Raises `ToolInputError` when it is not a string, names no Python file of the graph or no
    definition of the file, and says what is defined near it; `PathEscapeError` when its path
    leaves the root.
    """
    if not isinstance(symbol, str) or not symbol:
        raise ToolInputError("symbol must be a string such as path/to/module.py::Class.method")
    path, sep, qual = symbol.partition("::")
    rel = resolve_inside(g.root, path).as_posix()
    if rel in g.skipped:
        raise ToolInputError(f"not read ({g.skipped[rel]}): {path!r}")
    if not rel.endswith(".py"):
        raise ToolInputError(
            f"not a Python file: {path!r}",
            hint="deps.call_graph reads Python only; use deps.graph or deps.impact for TypeScript",
        )
    if rel not in g.definitions:
        near = difflib.get_close_matches(
            rel, [d for d, n in g.definitions.items() if n == 0], _MAX_NEAR
        )
        raise ToolInputError(f"no such Python file: {path!r}", hint=_near("nearest files", near))
    wanted = f"{rel}::{qual}" if sep else rel
    if wanted not in g.definitions:
        here = sorted(d for d in g.definitions if d.startswith(f"{rel}::"))
        last = qual.rpartition(".")[2]
        same = [d for d in sorted(g.definitions) if _last(d) == last]
        close = difflib.get_close_matches(wanted, here, _MAX_NEAR)
        near = list(dict.fromkeys([*same, *close, *here]))[:_MAX_NEAR]
        raise ToolInputError(
            f"{rel} does not define {qual!r}",
            hint=_near("defined near it", near) if near else f"{rel} defines nothing",
        )
    return wanted


def _near(label: str, names: list[str]) -> str:
    return f"{label}: {', '.join(names)}" if names else "give a Python file in the work tree"


def _last(symbol: str) -> str:
    return symbol.partition("::")[2].rpartition(".")[2]


@dataclass
class Walk:
    """The edges one query reached, each with its depth, and the symbols it did and did not expand."""

    edges: list[tuple[int, CallEdge]] = field(default_factory=list)
    expanded: set[str] = field(default_factory=set)
    cut: set[str] = field(default_factory=set)


def walk(g: CallGraph, symbol: str, *, callers: bool, depth: int) -> Walk:
    """Follow the edges of `g` from `symbol`, backwards when `callers`, up to `depth` calls away.

    Each symbol is expanded once, so a cycle ends. A class stands for its constructor when its
    callees are asked for. The edges are sorted by depth, then by where the call is written.
    """
    index: dict[str, list[CallEdge]] = {}
    for edge in g.edges:
        index.setdefault(edge.callee if callers else edge.caller, []).append(edge)
    result = Walk()
    frontier = {symbol}
    if not callers and symbol in g.constructors:
        frontier.add(g.constructors[symbol])
    for level in range(1, depth + 1):
        result.expanded |= frontier
        reached = [e for s in frontier for e in index.get(s, [])]
        result.edges += [(level, e) for e in reached]
        frontier = {e.caller if callers else e.callee for e in reached} - result.expanded
    result.cut = {s for s in frontier if index.get(s)}
    result.edges.sort()
    return result


def call_graph(
    repo_path: str | Path,
    symbol: str,
    *,
    direction: str = "both",
    depth: int = DEFAULT_DEPTH,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Return the callers, the callees or both of one Python `symbol` as text.

    `symbol` is `path::Qualified.name`, or a path alone for a module's top-level code. `depth` is
    how many calls away to follow, 1 to `MAX_DEPTH`. The sections, each sorted and each with its
    count, are `callers` and `callees`, with one `depth path:line caller -> callee` line per call;
    `unresolved`, with one `path:line caller 'expression' (reason)` line per call that no one
    definition could be tied to; and `references`, with one `path:line holder uses target as a
    value` line. Then come the skipped files and notes on what is missing. Raises `ToolInputError`
    for a bad argument or a symbol the repository does not define, `PathEscapeError` for a path
    outside the root, and what `tools.deps.build_graph` raises. The output is truncated to
    `max_bytes`.
    """
    if direction not in DIRECTIONS:
        raise ToolInputError(f"direction must be one of {', '.join(DIRECTIONS)}")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= MAX_DEPTH:
        raise ToolInputError(f"depth must be an integer from 1 to {MAX_DEPTH}")
    g = build_call_graph(repo_path)
    target = check_symbol(g, symbol)
    return truncate(render(g, target, direction, depth).encode(), max_bytes)


def render(g: CallGraph, symbol: str, direction: str, depth: int) -> str:
    """Return the text of one query; see `call_graph` for its form."""
    unresolved: set[UnresolvedCall] = set()
    references = {r for r in g.references if r.target == symbol}
    cut: set[str] = set()
    body: list[str] = []
    if direction != "callees":
        found = walk(g, symbol, callers=True, depth=depth)
        body += _section("callers", (f"{n} {_edge(e)}" for n, e in found.edges))
        wanted = {_last(s) for s in found.expanded if "::" in s}
        unresolved |= {u for u in g.unresolved if u.name in wanted}
        cut |= found.cut
    external = 0
    if direction != "callers":
        found = walk(g, symbol, callers=False, depth=depth)
        body += _section("callees", (f"{n} {_edge(e)}" for n, e in found.edges))
        unresolved |= {u for u in g.unresolved if u.caller in found.expanded}
        references |= {r for r in g.references if r.holder in found.expanded}
        external = sum(g.external[s] for s in found.expanded)
        cut |= found.cut
    body += _section(
        "unresolved",
        (f"{u.path}:{u.line} {u.caller} {u.expression!r} ({u.reason})" for u in sorted(unresolved)),
    )
    body += _section(
        "references",
        (f"{r.path}:{r.line} {r.holder} uses {r.target} as a value" for r in sorted(references)),
    )
    body += [f"skipped {path} ({reason})" for path, reason in sorted(g.skipped.items())]
    if external:
        body.append(f"note: {external} external calls are not listed")
    if cut:
        body.append(f"note: depth limit reached, {len(cut)} symbols with more calls not followed")
    if direction != "callees":
        if "::" not in symbol:
            body.append("note: a module is imported, not called; use deps.impact for its importers")
        if g.unresolved:
            body.append(
                f"note: {len(g.unresolved)} unresolved calls in the repository, so callers may be "
                "missing; the ones that call this name are listed as unresolved"
            )
    return f"call graph of {symbol}: {direction}, depth {depth}\n" + "\n".join(body) + "\n"


def _edge(edge: CallEdge) -> str:
    return f"{edge.path}:{edge.line} {edge.caller} -> {edge.callee}"


def _section(title: str, lines: Iterator[str]) -> list[str]:
    listed = list(lines)
    return [f"{title}: {len(listed)}", *listed]


CALL_GRAPH = ToolSpec(
    name="deps.call_graph",
    description="Return the callers and callees of one Python symbol, path::Qualified.name (a path alone is the module's top-level code), as one line per call with depth, path, line, caller and callee. Python only, read with the ast module; nothing is run. A call is listed as an edge only when the source ties it to one definition: a defined or imported name, a name on an imported module, self, cls or super(), or a class and the object built from it. Every other call, such as computed getattr, a value taken from a dict or list, a callable parameter or a method of an object of unknown type, is listed as unresolved with a reason, never guessed, so the callers may be incomplete. A function used as a value is listed as a reference, not as a call. Calls of builtins and installed packages are counted only. A subclass override, a property access, an operator and a with statement are not seen as calls.",
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "path::Qualified.name relative to the repository root, such as app/core.py::Ledger.post or app/core.py::outer.inner, or a path alone for the module's top-level code."},
            "direction": {"type": "string", "enum": list(DIRECTIONS), "description": "Which calls to follow. Default both."},
            "depth": {"type": "integer", "minimum": 1, "maximum": MAX_DEPTH, "description": f"How many calls away to follow, 1 for direct calls only. Default {DEFAULT_DEPTH}."},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    impl=call_graph,
)  # fmt: skip
"""The spec of `deps.call_graph`, which `tools.deps` declares in its `TOOLS`."""
