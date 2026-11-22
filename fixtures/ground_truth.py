"""Schema for `ground-truth.yaml`, what is known about a decomposition fixture, and its checker.

A fixture that code-mapping agents and tools run on carries this file beside its `defects.yaml`
(`docs/decisions/0005-decomposition-target.md`). The dead code is in `defects.yaml`, as entries of
the category `dead-code`; this file adds the symbol behind each of those entries and everything
else that is known: the module map, the decoys, the call chains, the data flows, the dynamic calls
and the features. Every entry has an `id`, unique in the file, that an eval check can name.

A symbol is written `path::Qualified.name`, the form `deps.call_graph` takes. A name assigned at
the top of a module counts as a symbol here. `path` alone means the module. The checker parses the
tree with `ast` and shares no code with the tools, so it can judge them.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from fixtures.schema import DefectManifest, FixtureManifestError, Slug, _Model

DEAD_CODE_CATEGORY = "dead-code"
"""The category in `defects.yaml` that marks an entry as planted dead code."""

SYMBOL_PATTERN = r"^(?!/)[^:\s\\]+\.py(::[A-Za-z_]\w*(\.[A-Za-z_]\w*)*)?$"

type Symbol = Annotated[str, Field(pattern=SYMBOL_PATTERN)]
type Text = Annotated[str, Field(min_length=1)]


class Site(_Model):
    """A place in the code: `snippet` is text that must appear inside the source of `symbol`."""

    symbol: Symbol
    snippet: Text


class Module(_Model):
    """One module of the package: what it is for and which modules of the package it imports.

    `imports` holds paths, sorted, and counts an import wherever it stands, inside a function too.
    """

    id: Slug
    path: Symbol
    role: Text
    imports: tuple[Symbol, ...]


class DeadCode(_Model):
    """The symbol behind the `defects.yaml` entry with the same `id`."""

    id: Slug
    symbol: Symbol
    kind: Literal["function", "class", "method", "module"]


class Decoy(_Model):
    """A symbol that looks dead and is not. `evidence` is the place that keeps it alive.

    `test_only` is called from the tests alone, `dispatch_table` is a value of a dictionary keyed
    by strings, `reexport` is imported by a package's `__init__.py` for others, `dynamic_lookup`
    is found by a name built at run time, and `callback` is registered as a value.
    """

    id: Slug
    kind: Literal["test_only", "dispatch_table", "reexport", "dynamic_lookup", "callback"]
    symbol: Symbol
    evidence: Site
    why: Text


class Call(_Model):
    """One call: `snippet` is the call as it stands in the source of `caller`."""

    caller: Symbol
    callee: Symbol
    snippet: Text


class CallChain(_Model):
    """A path of calls. Each call starts where the one before it ended."""

    id: Slug
    title: Text
    calls: tuple[Call, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _connected(self) -> CallChain:
        for before, after in zip(self.calls, self.calls[1:], strict=False):
            if before.callee != after.caller:
                raise ValueError(f"{self.id}: {after.caller} does not follow {before.callee}")
        return self


class FlowStep(Site):
    """One step of a data flow: what happens to the data at this place."""

    does: Text


class DataFlow(_Model):
    """How one piece of data travels, in order, through at least two modules."""

    id: Slug
    data: Text
    steps: tuple[FlowStep, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _crosses_modules(self) -> DataFlow:
        if len({_path(step.symbol) for step in self.steps}) < 2:
            raise ValueError(f"{self.id}: a data flow passes through at least two modules")
        return self


class DynamicCall(Site):
    """A call no static reading can tie to one definition, and what it reaches at run time."""

    id: Slug
    reason: Text
    targets: tuple[Symbol, ...] = Field(min_length=1)


class Feature(_Model):
    """One feature bound into the monolith, with everything an extraction has to move or cut.

    `symbols` belong to the feature and `modules` are the files they are in. `uses` are symbols
    outside it that it depends on, `called_from` the places outside it that call it, `state` where
    it touches shared state, and `tests` the files that pin its behaviour.
    """

    id: Slug
    title: Text
    modules: tuple[Symbol, ...] = Field(min_length=1)
    symbols: tuple[Symbol, ...] = Field(min_length=1)
    uses: tuple[Symbol, ...]
    called_from: tuple[Site, ...] = Field(min_length=1)
    state: tuple[Site, ...]
    tests: tuple[Symbol, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _modules_hold_the_symbols(self) -> Feature:
        held = sorted({_path(symbol) for symbol in self.symbols})
        if sorted(self.modules) != held:
            raise ValueError(f"{self.id}: modules must be the files of symbols: {', '.join(held)}")
        return self


class GroundTruth(_Model):
    """The whole `ground-truth.yaml` of one fixture. `package` is the directory the map covers."""

    fixture: Slug
    package: Text
    modules: tuple[Module, ...] = Field(min_length=1)
    dead_code: tuple[DeadCode, ...]
    decoys: tuple[Decoy, ...]
    call_chains: tuple[CallChain, ...]
    data_flows: tuple[DataFlow, ...]
    dynamic_calls: tuple[DynamicCall, ...]
    features: tuple[Feature, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> GroundTruth:
        ids = [entry.id for entry in self.entries()]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate ids: {', '.join(dupes)}")
        return self

    def entries(
        self,
    ) -> Iterator[Module | DeadCode | Decoy | CallChain | DataFlow | DynamicCall | Feature]:
        """Yield every entry that has an id, in file order."""
        yield from self.modules
        yield from self.dead_code
        yield from self.decoys
        yield from self.call_chains
        yield from self.data_flows
        yield from self.dynamic_calls
        yield from self.features


def load_ground_truth(path: Path, *, expected_fixture: str | None = None) -> GroundTruth:
    """Read and validate the `ground-truth.yaml` at `path`.

    Raises `FixtureManifestError` when the file is missing, is not valid YAML, fails the schema, or
    names a fixture other than `expected_fixture`.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FixtureManifestError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise FixtureManifestError(f"{path} is not valid YAML: {exc}") from exc
    try:
        truth = GroundTruth.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise FixtureManifestError(f"{path} is invalid: {details}") from exc
    if expected_fixture is not None and truth.fixture != expected_fixture:
        raise FixtureManifestError(
            f"{path} names fixture {truth.fixture!r}, expected {expected_fixture!r}",
            hint="the fixture field must equal the fixture's directory name",
        )
    return truth


def check_ground_truth(
    truth: GroundTruth,
    defects: DefectManifest,
    files: Iterable[str],
    read_file: Callable[[str], str | None],
) -> None:
    """Check every entry against the head tree, and the dead code against `defects`.

    `files` are the relative POSIX paths of the tree and `read_file(path)` returns the text of one,
    or None. Raises `FixtureManifestError` listing every problem: a symbol that is not defined, a
    snippet that is not in the source of its symbol, a call or a feature use whose name is not at
    the place given, a module missing from the map or with other imports, and a `dead-code` entry
    of `defects.yaml` whose symbol is missing here, has another window, or the other way round.
    """
    tree = _Tree(set(files), read_file)
    problems: list[str] = []
    for owner, symbol in _symbols(truth):
        if tree.span(symbol) is None:
            problems.append(f"{owner}: no {symbol} in the head tree")
    for owner, site in _sites(truth):
        source = tree.source(site.symbol)
        if source is not None and site.snippet not in source:
            problems.append(f"{owner}: snippet {site.snippet!r} is not in {site.symbol}")
    problems += _check_modules(truth, tree)
    problems += _check_dead_code(truth, defects, tree)
    problems += _check_calls(truth)
    problems += _check_features(truth, tree)
    if problems:
        raise FixtureManifestError(
            f"fixture {truth.fixture!r} does not match its head tree: " + "; ".join(problems),
            hint="update ground-truth.yaml after editing the source tree",
        )


def _path(symbol: str) -> str:
    """Return the file part of a symbol."""
    return symbol.partition("::")[0]


def _name(symbol: str) -> str:
    """Return the last name of a symbol, or the empty string for a module."""
    return symbol.partition("::")[2].rpartition(".")[2]


class _Tree:
    """The head tree, parsed on demand."""

    def __init__(self, files: set[str], read_file: Callable[[str], str | None]) -> None:
        self.files = files
        self._read = read_file
        self._parsed: dict[str, tuple[list[str], ast.Module] | None] = {}
        self._spans: dict[str, dict[str, tuple[int, int]]] = {}

    def module(self, path: str) -> ast.Module | None:
        """Return the parsed module at `path`, or None when it is missing or does not parse."""
        loaded = self._load(path)
        return None if loaded is None else loaded[1]

    def span(self, symbol: str) -> tuple[int, int] | None:
        """Return the first and last line of `symbol`, or None when it is not defined."""
        path, _, name = symbol.partition("::")
        loaded = self._load(path)
        if loaded is None:
            return None
        if not name:
            return 1, max(1, len(loaded[0]))
        if path not in self._spans:
            self._spans[path] = dict(_spans(loaded[1].body, ""))
        return self._spans[path].get(name)

    def source(self, symbol: str) -> str | None:
        """Return the source lines of `symbol`, or None when it is not defined."""
        span = self.span(symbol)
        loaded = self._load(_path(symbol))
        if span is None or loaded is None:
            return None
        return "\n".join(loaded[0][span[0] - 1 : span[1]])

    def _load(self, path: str) -> tuple[list[str], ast.Module] | None:
        if path not in self._parsed:
            text = self._read(path) if path in self.files else None
            parsed = None
            if text is not None:
                try:
                    parsed = text.splitlines(), ast.parse(text)
                except SyntaxError, ValueError:
                    parsed = None
            self._parsed[path] = parsed
        return self._parsed[path]


def _spans(body: list[ast.stmt], prefix: str) -> Iterator[tuple[str, tuple[int, int]]]:
    """Yield the qualified name and the lines, decorators included, of what `body` defines."""
    for node in body:
        end = node.end_lineno or node.lineno
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            yield prefix + node.name, (first, end)
            yield from _spans(node.body, f"{prefix}{node.name}.")
        elif not prefix and isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    yield target.id, (node.lineno, end)


def _symbols(truth: GroundTruth) -> Iterator[tuple[str, str]]:
    """Yield every symbol the file names, with the id of the entry that names it."""
    for module in truth.modules:
        yield module.id, module.path
        yield from ((module.id, path) for path in module.imports)
    for dead in truth.dead_code:
        yield dead.id, dead.symbol
    for decoy in truth.decoys:
        yield decoy.id, decoy.symbol
    for chain in truth.call_chains:
        yield from ((chain.id, call.callee) for call in chain.calls)
    for call in truth.dynamic_calls:
        yield from ((call.id, target) for target in call.targets)
    for feature in truth.features:
        for symbol in (*feature.symbols, *feature.uses, *feature.tests):
            yield feature.id, symbol
    for owner, site in _sites(truth):
        yield owner, site.symbol


def _sites(truth: GroundTruth) -> Iterator[tuple[str, Site]]:
    """Yield every place the file names, with the id of the entry that names it."""
    for decoy in truth.decoys:
        yield decoy.id, decoy.evidence
    for chain in truth.call_chains:
        for call in chain.calls:
            yield chain.id, Site(symbol=call.caller, snippet=call.snippet)
    for flow in truth.data_flows:
        yield from ((flow.id, step) for step in flow.steps)
    for call in truth.dynamic_calls:
        yield call.id, call
    for feature in truth.features:
        yield from ((feature.id, site) for site in (*feature.called_from, *feature.state))


def _check_modules(truth: GroundTruth, tree: _Tree) -> Iterator[str]:
    """Yield a problem for every module the map misses, and for wrong imports."""
    package = {
        path
        for path in tree.files
        if path.endswith(".py") and PurePosixPath(path).is_relative_to(truth.package)
    }
    mapped = [module.path for module in truth.modules]
    for path in sorted(package - set(mapped)):
        yield f"modules: {path} is not in the module map"
    for path in sorted({path for path in mapped if mapped.count(path) > 1 or path not in package}):
        yield f"modules: {path} is listed twice or is not in {truth.package}"
    for module in truth.modules:
        parsed = tree.module(module.path)
        if parsed is None:
            continue
        found = tuple(sorted(_imports(parsed, package)))
        if found != module.imports:
            yield f"{module.id}: imports are {', '.join(found) or 'none'}"


def _imports(module: ast.Module, package: set[str]) -> set[str]:
    """Return the files of `package` that absolute imports anywhere in `module` name."""
    found: set[str] = set()
    for node in ast.walk(module):
        dotted: list[str] = []
        if isinstance(node, ast.Import):
            dotted = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            dotted = [f"{node.module}.{alias.name}" for alias in node.names]
        for name in dotted:
            parts = name.split(".")
            for cut in (len(parts), len(parts) - 1):
                stem = "/".join(parts[:cut])
                hit = {f"{stem}.py", f"{stem}/__init__.py"} & package
                if hit:
                    found |= hit
                    break
    return found


def _check_dead_code(truth: GroundTruth, defects: DefectManifest, tree: _Tree) -> Iterator[str]:
    """Yield a problem for every difference between `dead_code` and the `dead-code` defects."""
    planted = {d.id: d for d in defects.defects if d.category == DEAD_CODE_CATEGORY}
    listed = {dead.id: dead for dead in truth.dead_code}
    for key in sorted(planted.keys() ^ listed.keys()):
        where = "dead_code" if key in planted else f"defects.yaml as {DEAD_CODE_CATEGORY}"
        yield f"{key}: is missing from {where}"
    for key in sorted(planted.keys() & listed.keys()):
        span = tree.span(listed[key].symbol)
        defect = planted[key]
        if span is None:
            continue
        if (defect.file, (defect.line_start, defect.line_end)) != (
            _path(listed[key].symbol),
            span,
        ):
            yield f"{key}: {listed[key].symbol} is at lines {span[0]}-{span[1]}, not at its defect"
        if (listed[key].kind == "module") == ("::" in listed[key].symbol):
            yield f"{key}: {listed[key].symbol} is not a {listed[key].kind}"


def _check_calls(truth: GroundTruth) -> Iterator[str]:
    """Yield a problem for every call whose snippet does not name its callee."""
    for chain in truth.call_chains:
        for call in chain.calls:
            if _name(call.callee) not in call.snippet:
                yield f"{chain.id}: snippet {call.snippet!r} does not name {call.callee}"


def _check_features(truth: GroundTruth, tree: _Tree) -> Iterator[str]:
    """Yield a problem for every feature use or caller that its source does not show."""
    for feature in truth.features:
        own = "\n".join(tree.source(symbol) or "" for symbol in feature.symbols)
        for symbol in feature.uses:
            if _name(symbol) not in own:
                yield f"{feature.id}: none of its symbols names {symbol}"
        for site in feature.called_from:
            if not any(_name(symbol) in site.snippet for symbol in feature.symbols):
                yield f"{feature.id}: {site.snippet!r} calls none of its symbols"
