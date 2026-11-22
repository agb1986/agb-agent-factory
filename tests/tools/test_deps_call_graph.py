"""Tests of `deps.call_graph` on the legacy-billing fixture and on small trees."""

import ast
import re
from pathlib import Path

import pytest
from repo_helpers import run_git

from fixtures import build as fixture_build
from tools import _call_graph, deps, registry
from tools._common import PathEscapeError, ToolInputError

EDGE = re.compile(r"(\d+) (\S+):(\d+) (\S+) -> (\S+)")


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write `files` into a new git work tree under `tmp_path` and return its root."""
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q")
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _edges(text: str) -> list[tuple[int, str, int, str, str]]:
    found = [EDGE.fullmatch(line) for line in text.splitlines()]
    return [(int(m[1]), m[2], int(m[3]), m[4], m[5]) for m in found if m]


def _section(text: str, title: str) -> list[str]:
    """Return the lines of one section, checking the count in its heading."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{title}: "))
    count = int(lines[start].removeprefix(f"{title}: "))
    return lines[start + 1 : start + 1 + count]


def _direct(root: Path, symbol: str) -> set[str]:
    """Return the direct callees of `symbol`."""
    return {e[4] for e in _edges(deps.call_graph(root, symbol, direction="callees", depth=1))}


def _reasons(root: Path, symbol: str) -> dict[str, str]:
    """Return the reason of each unresolved call `symbol` makes itself."""
    text = deps.call_graph(root, symbol, direction="callees", depth=1)
    found = [re.fullmatch(r"\S+ \S+ (.*) \((\w+)\)", s) for s in _section(text, "unresolved")]
    return {ast.literal_eval(m[1]): m[2] for m in found if m}


# --- the fixture --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def legacy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("legacy")
    (built,) = fixture_build.build(["legacy-billing"], dest=dest)
    return built.path


@pytest.fixture(scope="module")
def truth():
    return fixture_build.discover()["legacy-billing"].load_ground_truth()


def _line(root: Path, symbol: str, snippet: str) -> int:
    """Return the line of the only place in the file of `symbol` that holds `snippet`."""
    text = (root / symbol.partition("::")[0]).read_text(encoding="utf-8")
    assert text.count(snippet) == 1, (symbol, snippet)
    return text[: text.index(snippet)].count("\n") + 1


@pytest.mark.parametrize("chain_id", ["chain-billing-run", "chain-import-usage"])
def test_every_call_of_a_chain_is_an_edge_both_ways(legacy: Path, truth, chain_id: str) -> None:
    (chain,) = [c for c in truth.call_chains if c.id == chain_id]
    assert chain.calls
    for call in chain.calls:
        down = _edges(deps.call_graph(legacy, call.caller, direction="callees", depth=1))
        up = _edges(deps.call_graph(legacy, call.callee, direction="callers", depth=1))
        lines = {e[2] for e in down if e[3:] == (call.caller, call.callee)}
        assert lines, call
        assert lines == {e[2] for e in up if e[3:] == (call.caller, call.callee)}
        source = (legacy / call.caller.partition("::")[0]).read_text(encoding="utf-8").splitlines()
        first = call.snippet.strip().splitlines()[0]
        assert any(first in source[n - 1] for n in lines), call


def test_main_reaches_the_table_write_at_depth_six(legacy: Path) -> None:
    text = deps.call_graph(legacy, "legacy_billing/cli.py::main", direction="callees", depth=6)
    assert "legacy_billing/storage.py::_write_table" in {e[4] for e in _edges(text)}


def test_every_dynamic_call_is_unresolved_with_its_line(legacy: Path, truth) -> None:
    reasons = {}
    assert len(truth.dynamic_calls) == 3
    for call in truth.dynamic_calls:
        text = deps.call_graph(legacy, call.symbol, direction="callees", depth=1)
        path, line = call.symbol.partition("::")[0], _line(legacy, call.symbol, call.snippet)
        (found,) = [
            s for s in _section(text, "unresolved") if s.startswith(f"{path}:{line} {call.symbol} ")
        ]
        reasons[call.symbol] = found.rpartition("(")[2].rstrip(")")
        assert not {e[4] for e in _edges(text)} & set(call.targets)
    assert reasons == {
        "legacy_billing/reports.py::run_report": "container",
        "legacy_billing/billing.py::export_ledger": "getattr",
        "legacy_billing/billing.py::post_invoice": "container",
    }


@pytest.mark.parametrize(
    "symbol",
    [
        "legacy_billing/reports.py::report_balances",
        "legacy_billing/exporters.py::export_csv",
        "legacy_billing/audit.py::log_posting",
    ],
)
def test_a_dynamic_target_gets_no_invented_caller(legacy: Path, truth, symbol: str) -> None:
    assert symbol in {t for call in truth.dynamic_calls for t in call.targets}
    text = deps.call_graph(legacy, symbol, direction="callers")
    dynamic = {call.symbol for call in truth.dynamic_calls}
    assert not {e[3] for e in _edges(text)} & dynamic
    assert not [e for e in _edges(text) if not e[1].startswith("tests/")]
    assert "unresolved calls in the repository, so callers may be missing" in text


def test_a_function_used_as_a_value_is_a_reference_not_a_call(legacy: Path) -> None:
    for symbol, snippet in (
        ("legacy_billing/reports.py::report_balances", "REPORTS = {"),
        ("legacy_billing/audit.py::log_posting", "state.HOOKS.append(log_posting)"),
    ):
        path = symbol.partition("::")[0]
        text = deps.call_graph(legacy, symbol, direction="callers")
        found = [r for r in _section(text, "references") if r.startswith(f"{path}:")]
        lines = [int(r.partition(" ")[0].rpartition(":")[2]) for r in found]
        assert lines and all(r.endswith(f" uses {symbol} as a value") for r in found)
        start = _line(legacy, symbol, snippet)
        assert any(start <= n <= start + 12 for n in lines), (found, start)


# --- what resolves ------------------------------------------------------------------------------


def test_imports_aliases_and_same_module_calls_resolve(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "pkg/__init__.py": "from pkg.ledger import post as exported\n",
            "pkg/ledger.py": "def post(): pass\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/deep.py": "def dig(): pass\n",
            "app.py": (
                "import pkg\n"
                "import pkg.ledger\n"
                "import pkg.sub.deep as d\n"
                "from pkg import exported, ledger\n"
                "from pkg.ledger import post as p\n"
                "def here(): pass\n"
                "def a(): ledger.post()\n"
                "def b(): p()\n"
                "def c(): d.dig()\n"
                "def e(): pkg.ledger.post()\n"
                "def f(): exported()\n"
                "def g(): pkg.exported()\n"
                "def h(): here()\n"
                "def i():\n"
                "    from pkg.sub import deep\n"
                "    deep.dig()\n"
                "def j():\n"
                "    alias = here\n"
                "    alias()\n"
                "def k(): getattr(ledger, 'post')()\n"
            ),
        },
    )
    post, dig = "pkg/ledger.py::post", "pkg/sub/deep.py::dig"
    wanted = {"a": post, "b": post, "c": dig, "e": post, "f": post, "g": post}
    wanted |= {"h": "app.py::here", "i": dig, "j": "app.py::here", "k": post}
    for name, target in wanted.items():
        assert _direct(root, f"app.py::{name}") == {target}, name


def test_methods_and_constructors_resolve(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "base.py": "class Base:\n    def __init__(self): pass\n    def shared(self): pass\n",
            "app.py": (
                "from base import Base\n"
                "class Plain: pass\n"
                "class Child(Base):\n"
                "    def run(self):\n"
                "        self.step()\n"
                "        self.shared()\n"
                "        super().shared()\n"
                "    def step(self): pass\n"
                "    @classmethod\n"
                "    def make(cls): return cls()\n"
                "    @staticmethod\n"
                "    def loose(self): self.step()\n"
                "    def __call__(self): pass\n"
                "def use():\n"
                "    c = Child()\n"
                "    c.run()\n"
                "    Child.make()\n"
                "    c()\n"
                "    Plain()\n"
            ),
        },
    )
    assert _direct(root, "app.py::Child.run") == {"app.py::Child.step", "base.py::Base.shared"}
    assert _direct(root, "app.py::Child.make") == {"app.py::Child", "base.py::Base.__init__"}
    assert _direct(root, "app.py::Child.loose") == set()
    assert _direct(root, "app.py::use") == {
        "app.py::Child",
        "app.py::Child.__call__",
        "app.py::Child.make",
        "app.py::Child.run",
        "app.py::Plain",
        "base.py::Base.__init__",
    }
    up = _edges(deps.call_graph(root, "base.py::Base.__init__", direction="callers", depth=1))
    assert [(e[2], e[3]) for e in up] == [(10, "app.py::Child.make"), (15, "app.py::use")]


def test_the_callees_of_a_class_are_those_of_its_constructor(tmp_path: Path) -> None:
    text = "def f(): pass\nclass C:\n    def __init__(self):\n        f()\n"
    root = _repo(tmp_path, {"a.py": text})
    assert _direct(root, "a.py::C") == {"a.py::f"}


def test_nested_and_decorated_functions_keep_their_names(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "a.py": (
                "import functools\n"
                "def deco(f): return f\n"
                "def factory(n): return deco\n"
                "def target(): pass\n"
                "def outer():\n"
                "    def inner():\n"
                "        target()\n"
                "    inner()\n"
                "class C:\n"
                "    @deco\n"
                "    def method(self): target()\n"
                "    @functools.lru_cache\n"
                "    def cached(self): target()\n"
                "@factory(1)\n"
                "async def decorated(): target()\n"
            ),
        },
    )
    text = deps.call_graph(root, "a.py::target", direction="callers", depth=1)
    assert [e[3] for e in _edges(text)] == [
        "a.py::outer.inner",
        "a.py::C.method",
        "a.py::C.cached",
        "a.py::decorated",
    ]
    assert _direct(root, "a.py::outer") == {"a.py::outer.inner"}
    assert _direct(root, "a.py") == {"a.py::deco", "a.py::factory"}
    assert _reasons(root, "a.py") == {"factory(1)": "call_result"}


def test_calls_in_lambdas_and_comprehensions_belong_to_the_function(tmp_path: Path) -> None:
    text = (
        "def f(x): pass\n"
        "def g(items):\n"
        "    a = [f(i) for i in items]\n"
        "    b = [i() for i in items]\n"
        "    return lambda x: f(x)\n"
    )
    root = _repo(tmp_path, {"a.py": text})
    edges = _edges(deps.call_graph(root, "a.py::g", direction="callees"))
    assert [e[2] for e in edges] == [3, 5]
    assert _reasons(root, "a.py::g") == {"i()": "container"}


# --- what does not resolve ----------------------------------------------------------------------


def test_each_unresolved_reason(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "mod.py": "def f(): pass\n",
            "star.py": "def g(): pass\n",
            "bad.py": "def (:\n",
            "a.py": (
                "import third_party\n"
                "import mod\n"
                "import bad\n"
                "from star import *\n"
                "from . import gone\n"
                "from random import choice\n"
                "def f1(): pass\n"
                "if choice([1]):\n"
                "    def twice(): pass\n"
                "else:\n"
                "    def twice(x): pass\n"
                "twice = twice or f1\n"
                "class C(third_party.Base):\n"
                "    def m(self): self.other()\n"
                "class D:\n"
                "    def m(self): self.other()\n"
                "def calls(cb, *cbs, **kw):\n"
                "    getattr(mod, cb)()\n"
                "    {'k': f1}['k']()\n"
                "    for hook in cbs:\n"
                "        hook()\n"
                "    cb()\n"
                "    cb.method()\n"
                "    f1()()\n"
                "    (lambda: 1)()\n"
                "    with open(cb) as handle:\n"
                "        handle()\n"
                "    twice()\n"
                "    g()\n"
                "    mod.missing()\n"
                "    bad.thing()\n"
                "    gone()\n"
                "    third_party.go(len(cb))\n"
            ),
        },
    )
    assert _reasons(root, "a.py::calls") == {
        "getattr(mod, cb)()": "getattr",
        "{'k': f1}['k']()": "container",
        "hook()": "container",
        "cb()": "parameter",
        "cb.method()": "unknown_type",
        "f1()()": "call_result",
        "(lambda: 1)()": "expression",
        "handle()": "variable",
        "twice()": "ambiguous",
        "g()": "star_import",
        "mod.missing()": "not_defined",
        "bad.thing()": "import",
        "gone()": "import",
    }
    assert _reasons(root, "a.py::C.m") == {"self.other()": "inherited"}
    assert _reasons(root, "a.py::D.m") == {"self.other()": "not_defined"}


def test_a_name_with_two_values_or_a_replaced_attribute_is_not_guessed(tmp_path: Path) -> None:
    text = (
        "def f(): pass\n"
        "def g(): pass\n"
        "def pick(flag):\n"
        "    chosen = f\n"
        "    if flag:\n"
        "        chosen = g\n"
        "    chosen()\n"
        "class C:\n"
        "    def __init__(self, act): self.act = act\n"
        "    def act(self): pass\n"
        "    def run(self): self.act()\n"
        "def late():\n"
        "    global made\n"
        "    made = f\n"
        "made = g\n"
        "def use(): made()\n"
    )
    root = _repo(tmp_path, {"a.py": text})
    assert _reasons(root, "a.py::pick") == {"chosen()": "ambiguous"}
    assert _reasons(root, "a.py::C.run") == {"self.act()": "ambiguous"}
    assert _reasons(root, "a.py::use") == {"made()": "ambiguous"}


def test_callers_list_the_unresolved_calls_of_the_same_name(tmp_path: Path) -> None:
    text = "class C:\n    def post(self): pass\ndef f(x):\n    x.post()\n    x.other()\n"
    root = _repo(tmp_path, {"a.py": text})
    out = deps.call_graph(root, "a.py::C.post", direction="callers")
    assert _edges(out) == []
    assert _section(out, "unresolved") == ["a.py:4 a.py::f 'x.post()' (unknown_type)"]
    assert "note: 2 unresolved calls in the repository" in out


def test_external_calls_are_counted_and_not_listed(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"a.py": "import os\ndef f():\n    os.getcwd()\n    return len('a')\n"})
    out = deps.call_graph(root, "a.py::f", direction="callees")
    assert "note: 2 external calls are not listed" in out
    assert _section(out, "callees") == _section(out, "unresolved") == []


# --- depth, output and input --------------------------------------------------------------------

CHAIN = {"a.py": "".join(f"def f{n}():\n    f{n + 1}()\n" for n in range(12)) + "def f12(): pass\n"}


def test_recursion_and_a_cycle_end(tmp_path: Path) -> None:
    text = "def f():\n    f()\n    g()\ndef g():\n    f()\na = b\nb = a\ndef h(): a()\n"
    root = _repo(tmp_path, {"a.py": text})
    out = deps.call_graph(root, "a.py::f", depth=_call_graph.MAX_DEPTH)
    assert _section(out, "callees") == [
        "1 a.py:2 a.py::f -> a.py::f",
        "1 a.py:3 a.py::f -> a.py::g",
        "2 a.py:5 a.py::g -> a.py::f",
    ]
    assert _section(out, "callers") == [
        "1 a.py:2 a.py::f -> a.py::f",
        "1 a.py:5 a.py::g -> a.py::f",
        "2 a.py:3 a.py::f -> a.py::g",
    ]
    assert _reasons(root, "a.py::h") == {"a()": "variable"}


def test_the_depth_bound_holds(tmp_path: Path) -> None:
    root = _repo(tmp_path, CHAIN)
    out = deps.call_graph(root, "a.py::f0", direction="callees")
    assert [e[0] for e in _edges(out)] == list(range(1, _call_graph.DEFAULT_DEPTH + 1))
    assert "note: depth limit reached, 1 symbols with more calls not followed" in out
    out = deps.call_graph(root, "a.py::f0", direction="callees", depth=_call_graph.MAX_DEPTH)
    assert len(_edges(out)) == _call_graph.MAX_DEPTH
    out = deps.call_graph(root, "a.py::f9", direction="callees", depth=_call_graph.MAX_DEPTH)
    assert "depth limit" not in out


def test_the_output_is_truncated_and_stable(tmp_path: Path) -> None:
    root = _repo(tmp_path, CHAIN)
    full = deps.call_graph(root, "a.py::f0", depth=9)
    assert full == deps.call_graph(root, "a.py::f0", depth=9)
    cut = deps.call_graph(root, "a.py::f0", depth=9, max_bytes=100)
    assert cut.startswith(full[:100])
    assert "[truncated: " in cut and "limit 100 bytes]" in cut


@pytest.mark.parametrize("depth", [0, -1, _call_graph.MAX_DEPTH + 1, True, "2", 1.5])
def test_a_bad_depth_is_an_input_error(tmp_path: Path, depth) -> None:
    root = _repo(tmp_path, CHAIN)
    with pytest.raises(ToolInputError, match="depth must be an integer from 1 to 10"):
        deps.call_graph(root, "a.py::f0", depth=depth)


def test_a_bad_direction_is_an_input_error(tmp_path: Path) -> None:
    with pytest.raises(ToolInputError, match="direction must be one of"):
        deps.call_graph(_repo(tmp_path, CHAIN), "a.py::f0", direction="up")


def test_an_unknown_symbol_says_what_is_defined_near_it(tmp_path: Path) -> None:
    files = {"pkg/core.py": "class C:\n    def post(self): pass\ndef other(): pass\n"}
    root = _repo(tmp_path, files)
    with pytest.raises(ToolInputError, match="does not define 'post'") as caught:
        deps.call_graph(root, "pkg/core.py::post")
    assert "pkg/core.py::C.post" in (caught.value.hint or "")
    with pytest.raises(ToolInputError, match="no such Python file") as caught:
        deps.call_graph(root, "pkg/cor.py::C.post")
    assert "pkg/core.py" in (caught.value.hint or "")
    with pytest.raises(ToolInputError):
        deps.call_graph(root, "pkg/core.py::")
    for bad in ("", 3):
        with pytest.raises(ToolInputError, match="symbol must be a string"):
            deps.call_graph(root, bad)  # ty: ignore[invalid-argument-type]


def test_a_path_outside_the_root_is_an_input_error(tmp_path: Path) -> None:
    root = _repo(tmp_path, CHAIN)
    for symbol in ("../a.py::f0", "/etc/passwd.py::f"):
        with pytest.raises(PathEscapeError):
            deps.call_graph(root, symbol)


def test_a_typescript_symbol_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"a.ts": "export function f() {}\n"})
    with pytest.raises(ToolInputError, match="not a Python file") as caught:
        deps.call_graph(root, "a.ts::f")
    assert "reads Python only" in (caught.value.hint or "")


def test_a_file_that_does_not_parse_is_skipped(tmp_path: Path) -> None:
    files = {"a.py": "import bad\ndef f():\n    g()\ndef g(): pass\n", "bad.py": "def (:\n"}
    root = _repo(tmp_path, files)
    out = deps.call_graph(root, "a.py::f")
    assert "skipped bad.py (syntax_error)" in out
    assert [e[4] for e in _edges(out)] == ["a.py::g"]
    with pytest.raises(ToolInputError, match=r"not read \(syntax_error\)"):
        deps.call_graph(root, "bad.py::f")


def test_a_module_symbol_is_its_top_level_code(tmp_path: Path) -> None:
    files = {"a.py": "def f(): pass\nf()\nclass C:\n    x = f()\n"}
    root = _repo(tmp_path, files)
    out = deps.call_graph(root, "a.py")
    assert _section(out, "callees") == ["1 a.py:2 a.py -> a.py::f", "1 a.py:4 a.py -> a.py::f"]
    assert "a module is imported, not called" in out


def test_the_code_read_is_not_run(tmp_path: Path) -> None:
    mark = tmp_path / "ran"
    root = _repo(tmp_path, {"a.py": f"open({str(mark)!r}, 'w').close()\ndef f(): pass\n"})
    deps.call_graph(root, "a.py::f")
    assert not mark.exists()


def test_the_tool_is_declared_read_only() -> None:
    (spec,) = [s for s in deps.TOOLS if s.name == "deps.call_graph"]
    assert spec.impl is deps.call_graph
    assert spec.name in registry.READ_ONLY
    assert set(spec.input_schema["properties"]) == {"symbol", "direction", "depth"}
    assert spec.input_schema["additionalProperties"] is False
