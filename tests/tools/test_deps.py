"""Tests of the dependency tools on small Python and TypeScript repositories built per test."""

import json
from pathlib import Path

import pytest
from repo_helpers import run_git

from factory.cli import main
from tools import deps
from tools._common import PathEscapeError, ToolInputError, ToolNotFoundError

PYTHON = {
    # A package whose modules import each other in a cycle: core -> util -> helpers -> core.
    "app/__init__.py": "from app.core import run\n",
    "app/core.py": "import os\nimport requests\n\nfrom . import util\n\n\ndef run(): ...\n",
    "app/util.py": "from app import helpers\n",
    "app/helpers.py": "import app.core\n",
    # Imports the tools cannot tie to one file.
    "app/plugins.py": (
        "import importlib\n"
        "\n"
        "def load(name):\n"
        "    importlib.import_module('app.' + name)\n"
        "    return __import__('json')\n"
    ),
    "app/broken_refs.py": "import app.ghost\nfrom ... import far\n",
    # A script outside any package sees its own directory.
    "scripts/tool.py": "import helper\n",
    "scripts/helper.py": "",
    "bad.py": "def (:\n",
    "README.md": "not a module\n",
}

TYPESCRIPT = {
    "tsconfig.json": (
        "{\n  // comments and trailing commas are allowed\n"
        '  "compilerOptions": {"paths": {"lib/*": ["src/lib/*"],},},\n}\n'
    ),
    # A barrel that re-exports two modules.
    "src/index.ts": "export * from './a';\nexport { b } from \"./b\";\n",
    # a and b import each other.
    "src/a.ts": (
        "import { b } from './b';\n"
        "import fs from 'node:fs';\n"
        "import path from 'path';\n"
        "import React from 'react';\n"
        "import { thing } from '@scope/pkg';\n"
    ),
    "src/b.ts": "import type { A } from './a.js';\n// import { gone } from './gone';\n",
    "src/c.tsx": (
        "import x from '@/lib/x';\n"
        "import cfg from 'lib/config';\n"
        "/* import { z } from './z'; */\n"
        "export async function load() {\n"
        "  return await import('./a');\n"
        "}\n"
    ),
    "src/d.js": (
        "const b = require('./b');\n"
        "const other = require(name);\n"
        "import './styles.css';\n"
        "import u from './util';\n"
    ),
    "src/e.ts": (
        "import { x } from '../../outside';\n"
        "import q from './nope';\n"
        "import s from 'src/a';\n"
        "import abs from '/etc/x';\n"
    ),
    "src/styles.css": "body {}\n",
    "src/util/index.ts": "export const u = 1;\n",
}


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


def _edges(g: deps.Graph) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in g.edges}


def _unresolved(g: deps.Graph) -> set[tuple[str, str, str]]:
    return {(u.source, u.specifier, u.reason) for u in g.unresolved}


# --- Python


@pytest.fixture
def py_root(tmp_path: Path) -> Path:
    return _repo(tmp_path, PYTHON)


def test_python_edges_follow_absolute_relative_and_script_imports(py_root: Path) -> None:
    g = deps.build_graph(py_root)
    assert _edges(g) == {
        ("app/__init__.py", "app/core.py"),
        ("app/core.py", "app/util.py"),
        ("app/util.py", "app/helpers.py"),
        ("app/helpers.py", "app/core.py"),
        ("scripts/tool.py", "scripts/helper.py"),
    }
    assert g.modules["app/core.py"] == "python"
    assert "README.md" not in g.modules
    assert {e.line for e in g.edges if e.source == "app/core.py"} == {4}


def test_python_external_imports_are_not_edges(py_root: Path) -> None:
    g = deps.build_graph(py_root)
    external = {(x.source, x.specifier) for x in g.external}
    assert ("app/core.py", "os") in external
    assert ("app/core.py", "requests") in external
    assert ("app/plugins.py", "importlib") in external


def test_python_unresolved_imports_are_reported_not_guessed(py_root: Path) -> None:
    g = deps.build_graph(py_root)
    assert _unresolved(g) == {
        ("app/plugins.py", "<expr>", "dynamic"),
        ("app/plugins.py", "json", "dynamic"),
        ("app/broken_refs.py", "app.ghost", "not_found"),
        ("app/broken_refs.py", "from ... import far", "outside_root"),
    }


def test_a_python_file_that_does_not_parse_is_skipped(py_root: Path) -> None:
    g = deps.build_graph(py_root)
    assert g.skipped == {"bad.py": "syntax_error"}
    assert "bad.py" not in g.modules


def test_python_impact_walks_the_cycle_once(py_root: Path) -> None:
    g = deps.build_graph(py_root)
    assert deps.impact_of(g, ["app/core.py"]) == [
        ("app/__init__.py", 1),
        ("app/helpers.py", 1),
        ("app/util.py", 2),
    ]


def test_two_source_roots_with_the_same_package_are_ambiguous(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "one/pkg/__init__.py": "",
            "two/pkg/__init__.py": "",
            "user.py": "import pkg\n",
        },
    )
    g = deps.build_graph(root)
    assert _unresolved(g) == {("user.py", "pkg", "ambiguous")}
    assert not g.edges


# --- TypeScript and JavaScript


@pytest.fixture
def ts_root(tmp_path: Path) -> Path:
    return _repo(tmp_path, TYPESCRIPT)


def test_typescript_edges_resolve_suffixes_js_to_ts_and_index_files(ts_root: Path) -> None:
    g = deps.build_graph(ts_root)
    assert _edges(g) == {
        ("src/index.ts", "src/a.ts"),
        ("src/index.ts", "src/b.ts"),
        ("src/a.ts", "src/b.ts"),
        ("src/b.ts", "src/a.ts"),
        ("src/d.js", "src/b.ts"),
        ("src/d.js", "src/styles.css"),
        ("src/d.js", "src/util/index.ts"),
    }
    assert g.modules["src/d.js"] == "javascript"
    assert g.modules["src/c.tsx"] == "typescript"
    assert g.modules["src/styles.css"] == "other"


def test_typescript_comments_are_ignored_and_lines_are_kept(ts_root: Path) -> None:
    g = deps.build_graph(ts_root)
    specifiers = {u.specifier for u in g.unresolved} | {x.specifier for x in g.external}
    assert "./gone" not in specifiers and "./z" not in specifiers
    lines = {(e.source, e.target): e.line for e in g.edges}
    assert lines[("src/index.ts", "src/b.ts")] == 2
    assert lines[("src/d.js", "src/util/index.ts")] == 4


def test_typescript_builtins_and_packages_are_external(ts_root: Path) -> None:
    g = deps.build_graph(ts_root)
    external = {x.specifier for x in g.external if x.source == "src/a.ts"}
    assert external == {"node:fs", "path", "react", "@scope/pkg"}


def test_typescript_dynamic_imports_and_aliases_are_unresolved(ts_root: Path) -> None:
    g = deps.build_graph(ts_root)
    assert _unresolved(g) == {
        ("src/c.tsx", "@/lib/x", "alias"),
        ("src/c.tsx", "lib/config", "alias"),
        ("src/c.tsx", "./a", "dynamic"),
        ("src/d.js", "<expr>", "dynamic"),
        ("src/e.ts", "../../outside", "outside_root"),
        ("src/e.ts", "./nope", "not_found"),
        ("src/e.ts", "src/a", "alias"),
        ("src/e.ts", "/etc/x", "absolute"),
    }


def test_typescript_impact_goes_through_the_cycle_and_the_barrel(ts_root: Path) -> None:
    g = deps.build_graph(ts_root)
    assert deps.impact_of(g, ["src/b.ts"]) == [
        ("src/a.ts", 1),
        ("src/d.js", 1),
        ("src/index.ts", 1),
    ]
    assert deps.impact_of(g, ["src/util/index.ts"]) == [("src/d.js", 1)]


def test_a_symlinked_module_is_skipped_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.ts"
    outside.write_text("import x from './secret';\n")
    root = _repo(tmp_path, {"src/a.ts": "import l from './link';\n"})
    (root / "src" / "link.ts").symlink_to(outside)
    g = deps.build_graph(root)
    assert g.skipped == {"src/link.ts": "symlink"}
    assert _unresolved(g) == {("src/a.ts", "./link", "not_found")}


# --- the tool functions


def test_graph_returns_text_with_counts_edges_and_unresolved(ts_root: Path) -> None:
    text = deps.graph(ts_root)
    assert text.startswith("8 modules, 7 edges, 8 unresolved, 4 external\n")
    assert "src/d.js -> src/b.ts, src/styles.css, src/util/index.ts\n" in text
    assert "unresolved src/c.tsx:1 '@/lib/x' (alias)\n" in text


def test_graph_output_is_truncated_with_a_marker(ts_root: Path) -> None:
    text = deps.graph(ts_root, max_bytes=60)
    assert "[truncated:" in text
    assert len(text.encode()) < 200


def test_impact_returns_depths_and_a_note_about_unresolved_imports(py_root: Path) -> None:
    text = deps.impact(py_root, ["app/core.py", "./app/core.py"])
    assert text.splitlines() == [
        "3 modules import app/core.py",
        "1 app/__init__.py",
        "1 app/helpers.py",
        "2 app/util.py",
        "note: 4 unresolved imports in the repository, so the set may be incomplete",
    ]


def test_impact_refuses_a_path_outside_the_root(py_root: Path) -> None:
    with pytest.raises(PathEscapeError):
        deps.impact(py_root, ["../elsewhere.py"])


def test_impact_refuses_a_file_that_is_not_a_module(py_root: Path) -> None:
    with pytest.raises(ToolNotFoundError):
        deps.impact(py_root, ["README.md"])
    with pytest.raises(ToolNotFoundError):
        deps.impact(py_root, ["bad.py"])


@pytest.mark.parametrize("paths", [[], "app/core.py"])
def test_impact_refuses_a_malformed_list(py_root: Path, paths: object) -> None:
    with pytest.raises(ToolInputError):
        deps.impact(py_root, paths)  # ty: ignore[invalid-argument-type]


def test_the_tools_need_the_top_of_a_work_tree(tmp_path: Path, py_root: Path) -> None:
    with pytest.raises(ToolNotFoundError):
        deps.graph(tmp_path / "missing")
    with pytest.raises(ToolInputError):
        deps.graph(py_root / "app")


def test_the_tools_change_nothing(py_root: Path) -> None:
    before = run_git(py_root, "status", "--porcelain", "--ignored")
    deps.graph(py_root)
    deps.impact(py_root, ["app/core.py"])
    assert run_git(py_root, "status", "--porcelain", "--ignored") == before


# --- factory deps


def test_command_prints_the_graph_as_text(
    py_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["deps", str(py_root)]) == 0
    assert capsys.readouterr().out.startswith("8 modules, 5 edges, 4 unresolved, 3 external\n")


def test_command_prints_the_impact_set_as_json(
    py_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["deps", str(py_root), "--impact", "app/util.py", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert set(document) == {"modules", "edges", "unresolved", "external", "skipped", "impact"}
    assert document["impact"] == {
        "paths": ["app/util.py"],
        "impacted": [
            {"path": "app/core.py", "depth": 1},
            {"path": "app/__init__.py", "depth": 2},
            {"path": "app/helpers.py", "depth": 2},
        ],
    }


def test_command_maps_an_unknown_path_to_exit_3(
    py_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["deps", str(py_root), "--impact", "nope.py", "--json"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == 3
