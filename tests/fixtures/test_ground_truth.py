"""Tests for `fixtures.ground_truth` and for the ground truth of `legacy-billing`."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from fixtures import build as fixture_build
from fixtures.ground_truth import (
    DEAD_CODE_CATEGORY,
    GroundTruth,
    check_ground_truth,
    load_ground_truth,
)
from fixtures.schema import DefectManifest, FixtureManifestError

LEGACY = fixture_build.discover()["legacy-billing"]

# --- the schema and the checker, on a small tree -------------------------------------------------

FILES = {
    "app/__init__.py": "from app.core import public\n",
    "app/cli.py": (
        "from app import core\n"
        "\n"
        "\n"
        "def main(name):\n"
        "    import app.store\n"
        "\n"
        "    core.TABLE[name]()\n"
        '    getattr(core, "show_" + name)()\n'
        "    return core.run(app.store.PATH)\n"
    ),
    "app/core.py": (
        "from app.store import save\n"
        "\n"
        "\n"
        "def run(value):\n"
        "    save(value)\n"
        "\n"
        "\n"
        "def public(): ...\n"
        "\n"
        "\n"
        "def listed(): ...\n"
        "\n"
        "\n"
        "def show_all(): ...\n"
        "\n"
        "\n"
        "def tested(): ...\n"
        "\n"
        "\n"
        'TABLE: dict = {"listed": listed}\n'
        "\n"
        "\n"
        "class Box:\n"
        "    @property\n"
        "    def unused(self): ...\n"
    ),
    "app/store.py": 'PATH = "x"\n\n\ndef save(value):\n    print(PATH, value)\n',
    "tests/test_core.py": "from app.core import tested\n\ntested()\n",
    "broken.py": "def (\n",
}

DEFECTS = {
    "fixture": "small",
    "summary": "A small tree.",
    "defects": [
        {
            "id": "dead-method",
            "file": "app/core.py",
            "line_start": 24,
            "line_end": 25,
            "severity": "minor",
            "category": DEAD_CODE_CATEGORY,
            "title": "Unused",
            "description": "Nothing reads it.",
            "snippet": "def unused",
        },
        {
            "id": "a-bug",
            "file": "app/core.py",
            "line_start": 4,
            "line_end": 5,
            "severity": "minor",
            "category": "logic",
            "title": "A bug",
            "description": "Not dead code, so the ground truth does not list it.",
            "snippet": "save(value)",
        },
    ],
}

TRUTH: dict[str, Any] = {
    "fixture": "small",
    "package": "app",
    "modules": [
        {
            "id": "module-init",
            "path": "app/__init__.py",
            "role": "Re-exports.",
            "imports": ["app/core.py"],
        },
        {
            "id": "module-cli",
            "path": "app/cli.py",
            "role": "The entry point.",
            "imports": ["app/core.py", "app/store.py"],
        },
        {
            "id": "module-core",
            "path": "app/core.py",
            "role": "The work.",
            "imports": ["app/store.py"],
        },
        {"id": "module-store", "path": "app/store.py", "role": "Storage.", "imports": []},
    ],
    "dead_code": [{"id": "dead-method", "symbol": "app/core.py::Box.unused", "kind": "method"}],
    "decoys": [
        {
            "id": "decoy-test",
            "kind": "test_only",
            "symbol": "app/core.py::tested",
            "evidence": {"symbol": "tests/test_core.py", "snippet": "tested()"},
            "why": "Only a test calls it.",
        },
        {
            "id": "decoy-table",
            "kind": "dispatch_table",
            "symbol": "app/core.py::listed",
            "evidence": {"symbol": "app/core.py::TABLE", "snippet": '"listed": listed'},
            "why": "It is in the table.",
        },
        {
            "id": "decoy-reexport",
            "kind": "reexport",
            "symbol": "app/core.py::public",
            "evidence": {"symbol": "app/__init__.py", "snippet": "import public"},
            "why": "The package re-exports it.",
        },
        {
            "id": "decoy-lookup",
            "kind": "dynamic_lookup",
            "symbol": "app/core.py::show_all",
            "evidence": {"symbol": "app/cli.py::main", "snippet": '"show_" + name'},
            "why": "Its name is built at run time.",
        },
    ],
    "call_chains": [
        {
            "id": "chain",
            "title": "From the entry point to storage",
            "calls": [
                {
                    "caller": "app/cli.py::main",
                    "callee": "app/core.py::run",
                    "snippet": "core.run(app.store.PATH)",
                },
                {
                    "caller": "app/core.py::run",
                    "callee": "app/store.py::save",
                    "snippet": "save(value)",
                },
            ],
        }
    ],
    "data_flows": [
        {
            "id": "flow",
            "data": "The path",
            "steps": [
                {"symbol": "app/cli.py::main", "snippet": "app.store.PATH", "does": "Reads it."},
                {"symbol": "app/store.py::save", "snippet": "print(PATH", "does": "Prints it."},
            ],
        }
    ],
    "dynamic_calls": [
        {
            "id": "dynamic",
            "symbol": "app/cli.py::main",
            "snippet": "core.TABLE[name]()",
            "reason": "Picked by a string.",
            "targets": ["app/core.py::listed"],
        }
    ],
    "features": [
        {
            "id": "feature",
            "title": "Running",
            "modules": ["app/core.py"],
            "symbols": ["app/core.py::run"],
            "uses": ["app/store.py::save"],
            "called_from": [{"symbol": "app/cli.py::main", "snippet": "core.run("}],
            "state": [{"symbol": "app/core.py::run", "snippet": "save(value)"}],
            "tests": ["tests/test_core.py"],
        }
    ],
}


def _changed(path: str, value: Any) -> dict[str, Any]:
    """Return a copy of `TRUTH` with the value at the dotted `path` replaced."""
    raw = copy.deepcopy(TRUTH)
    *parents, last = [int(part) if part.isdigit() else part for part in path.split(".")]
    node: Any = raw
    for part in parents:
        node = node[part]
    node[last] = value
    return raw


def _check(
    raw: dict[str, Any], defects: dict[str, Any] = DEFECTS, files: dict[str, str] = FILES
) -> None:
    check_ground_truth(
        GroundTruth.model_validate(raw), DefectManifest.model_validate(defects), files, files.get
    )


def test_valid_truth_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "ground-truth.yaml"
    path.write_text(yaml.safe_dump(TRUTH, sort_keys=False))
    truth = load_ground_truth(path, expected_fixture="small")
    assert GroundTruth.model_validate_json(truth.model_dump_json()) == truth
    assert [entry.id for entry in truth.entries()] == [
        "module-init",
        "module-cli",
        "module-core",
        "module-store",
        "dead-method",
        "decoy-test",
        "decoy-table",
        "decoy-reexport",
        "decoy-lookup",
        "chain",
        "flow",
        "dynamic",
        "feature",
    ]
    _check(TRUTH)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("modules.0.id", "Not A Slug", "modules.0.id"),
        ("modules.0.id", "chain", "duplicate ids: chain"),
        ("modules.0.path", "/app/cli.py", "modules.0.path"),
        ("modules.0.path", "app\\cli.py", "modules.0.path"),
        ("dead_code.0.symbol", "app/core.py::Box..unused", "dead_code.0.symbol"),
        ("dead_code.0.kind", "import", "dead_code.0.kind"),
        ("decoys.0.kind", "unknown", "decoys.0.kind"),
        ("decoys.0.why", "", "decoys.0.why"),
        ("call_chains.0.calls", [], "call_chains.0.calls"),
        ("call_chains.0.calls.1.caller", "app/cli.py::main", "does not follow"),
        ("data_flows.0.steps.0.symbol", "app/store.py::save", "at least two modules"),
        ("dynamic_calls.0.targets", [], "dynamic_calls.0.targets"),
        ("features.0.modules", ["app/cli.py"], "modules must be the files of symbols: app/core.py"),
        ("features.0.tests", [], "features.0.tests"),
        ("extra", 1, "extra"),
        ("fixture", "other", "names fixture 'other', expected 'small'"),
    ],
)
def test_invalid_file_is_refused(tmp_path: Path, path: str, value: Any, message: str) -> None:
    file = tmp_path / "ground-truth.yaml"
    file.write_text(yaml.safe_dump(_changed(path, value)))
    with pytest.raises(FixtureManifestError, match=re.escape(message)):
        load_ground_truth(file, expected_fixture="small")


def test_unreadable_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ground-truth.yaml"
    with pytest.raises(FixtureManifestError, match="cannot read"):
        load_ground_truth(path)
    path.write_text("a: b: c\n")
    with pytest.raises(FixtureManifestError, match="not valid YAML"):
        load_ground_truth(path)
    path.write_text("")
    with pytest.raises(FixtureManifestError, match="<root>"):
        load_ground_truth(path)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("modules.3.path", "app/gone.py", "module-store: no app/gone.py in the head tree"),
        ("modules.3.path", "app/gone.py", "app/store.py is not in the module map"),
        ("modules.3.path", "app/core.py", "app/core.py is listed twice"),
        ("modules.3.path", "tests/test_core.py", "is listed twice or is not in app"),
        ("modules.3.path", "broken.py", "module-store: no broken.py in the head tree"),
        ("modules.2.imports", [], "module-core: imports are app/store.py"),
        ("modules.3.imports", ["app/core.py"], "module-store: imports are none"),
        ("dead_code.0.id", "dead-other", "dead-other: is missing from defects.yaml as dead-code"),
        ("dead_code", [], "dead-method: is missing from dead_code"),
        ("dead_code.0.id", "a-bug", "a-bug: is missing from defects.yaml as dead-code"),
        ("dead_code.0.symbol", "app/core.py::Box.gone", "no app/core.py::Box.gone"),
        ("dead_code.0.symbol", "app/gone.py", "dead-method: no app/gone.py"),
        ("dead_code.0.symbol", "app/core.py::Box", "is at lines 23-25, not at its defect"),
        ("dead_code.0.kind", "function", "app/core.py::Box.unused is not a function"),
        ("decoys.0.symbol", "app/core.py::gone", "decoy-test: no app/core.py::gone"),
        ("decoys.0.symbol", "app/core.py::Box.unused", "is listed as dead code"),
        ("decoys.0.symbol", "app/core.py::run", "does not name app/core.py::run"),
        ("decoys.0.evidence.symbol", "tests/gone.py", "decoy-test: no tests/gone.py"),
        ("decoys.0.evidence.symbol", "app/core.py", "test_only decoy is outside app"),
        (
            "decoys.1.evidence.snippet",
            "listed, gone",
            "'listed, gone' is not in app/core.py::TABLE",
        ),
        ("decoys.1.evidence.symbol", "app/core.py::run", "is not in app/core.py::run"),
        ("decoys.2.evidence.symbol", "app/core.py", "reexport decoy is in an __init__.py"),
        ("decoys.3.evidence.snippet", "show_all", "'show_all' names app/core.py::show_all"),
        ("call_chains.0.calls.1.callee", "app/store.py::gone", "chain: no app/store.py::gone"),
        ("call_chains.0.calls.1.callee", "app/store.py::PATH", "does not name app/store.py::PATH"),
        ("call_chains.0.calls.1.snippet", "save(other)", "chain: snippet 'save(other)' is not in"),
        ("data_flows.0.steps.1.snippet", "gone", "flow: snippet 'gone' is not in"),
        ("data_flows.0.steps.1.symbol", "app/store.py::gone", "flow: no app/store.py::gone"),
        ("dynamic_calls.0.snippet", "gone()", "dynamic: snippet 'gone()' is not in"),
        ("dynamic_calls.0.targets", ["app/core.py::gone"], "dynamic: no app/core.py::gone"),
        ("features.0.symbols", ["app/core.py::gone"], "feature: no app/core.py::gone"),
        ("features.0.uses", ["app/store.py::PATH"], "none of its symbols names app/store.py::PATH"),
        ("features.0.uses", ["app/cli.py"], "none of its symbols names app/cli.py"),
        ("features.0.called_from.0.snippet", "def main(name)", "calls none of its symbols"),
        ("features.0.state.0.snippet", "gone", "feature: snippet 'gone' is not in"),
        ("features.0.tests", ["tests/gone.py"], "feature: no tests/gone.py"),
    ],
)
def test_stale_truth_is_refused(path: str, value: Any, message: str) -> None:
    with pytest.raises(FixtureManifestError, match=re.escape(message)):
        _check(_changed(path, value))


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [
        ("app/core.py", "def listed(): ...", "def listing(): ...", "no app/core.py::listed"),
        ("app/core.py", "def run", "X = 1\n\n\ndef run", "not at its defect"),
        ("app/cli.py", "    import app.store\n", "", "module-cli: imports are app/core.py"),
        ("app/store.py", "PATH = ", "def = ", "no app/store.py::save"),
    ],
)
def test_changed_tree_is_refused(path: str, old: str, new: str, message: str) -> None:
    assert old in FILES[path]
    files = {**FILES, path: FILES[path].replace(old, new, 1)}
    with pytest.raises(FixtureManifestError, match=re.escape(message)):
        _check(TRUTH, files=files)


def test_unmapped_module_is_refused() -> None:
    with pytest.raises(FixtureManifestError, match="app/extra.py is not in the module map"):
        _check(TRUTH, files={**FILES, "app/extra.py": ""})


def test_stale_truth_fails_the_build(tmp_path: Path) -> None:
    source = tmp_path / "src" / "small"
    for rel, text in FILES.items():
        for ref in ("base", "head"):
            target = source / ref / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    (source / "head" / "README.md").write_text("Changed.\n")
    (source / "defects.yaml").write_text(yaml.safe_dump(DEFECTS))
    truth = source / "ground-truth.yaml"

    def build() -> list[fixture_build.BuiltFixture]:
        return list(
            fixture_build.build(["small"], dest=tmp_path / "repos", roots=[tmp_path / "src"])
        )

    truth.write_text(yaml.safe_dump(TRUTH))
    assert build()
    truth.write_text(yaml.safe_dump(_changed("decoys.0.symbol", "app/core.py::gone")))
    with pytest.raises(FixtureManifestError, match="no app/core.py::gone"):
        build()


def test_a_fixture_without_the_file_has_no_truth() -> None:
    assert fixture_build.discover()["storefront"].load_ground_truth() is None


# --- legacy-billing ------------------------------------------------------------------------------


def _legacy() -> GroundTruth:
    truth = LEGACY.load_ground_truth()
    assert truth is not None
    return truth


def _sources(ref: str) -> dict[str, str]:
    """Return every Python file of one ref of `legacy-billing`, by its path in the tree."""
    root = LEGACY.source / ref
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
    }


def _without_dead_code(sources: dict[str, str]) -> dict[str, str]:
    """Return the tree with the lines of every `dead-code` defect blanked."""
    live = {path: text.splitlines() for path, text in sources.items()}
    for defect in LEGACY.load_manifest().defects:
        assert defect.category == DEAD_CODE_CATEGORY
        for line in range(defect.line_start - 1, defect.line_end):
            live[defect.file][line] = ""
    return {path: "\n".join(lines) for path, lines in live.items()}


def _places(name: str, sources: dict[str, str]) -> list[tuple[str, str]]:
    """Return the path and the text of every line that holds `name` as a word, its `def` aside."""
    word = re.compile(rf"\b{re.escape(name)}\b")
    define = re.compile(rf"\s*(def|class) {re.escape(name)}\b")
    return [
        (path, line.strip())
        for path, text in sources.items()
        for line in text.splitlines()
        if word.search(line) and not define.match(line)
    ]


def test_legacy_truth_matches_both_refs() -> None:
    truth, defects = _legacy(), LEGACY.load_manifest()
    for ref in ("base", "head"):
        sources = _sources(ref)
        check_ground_truth(truth, defects, sources, sources.get)


def test_legacy_head_only_changes_the_readme() -> None:
    assert _sources("base") == _sources("head")


def test_legacy_holds_what_adr_0005_asks_for() -> None:
    truth = _legacy()
    assert 15 <= len(truth.modules) <= 25
    assert {dead.kind for dead in truth.dead_code} == {"function", "class", "method", "module"}
    assert {d.id for d in truth.dead_code} >= {"dead-through-dead"}
    assert {decoy.kind for decoy in truth.decoys} >= {"test_only", "dispatch_table", "reexport"}
    deepest = max(truth.call_chains, key=lambda chain: len(chain.calls))
    assert len(deepest.calls) >= 4
    assert deepest.calls[0].caller == "legacy_billing/cli.py::main"
    assert deepest.calls[-1].callee.startswith("legacy_billing/storage.py::")
    assert truth.data_flows
    assert len(truth.features) == 2
    monolith = "legacy_billing/billing.py"
    assert all(monolith in feature.modules for feature in truth.features)
    first, second = truth.features
    assert not set(first.tests) & set(second.tests)
    assert not set(first.symbols) & set(second.symbols)


@pytest.mark.parametrize("dead", _legacy().dead_code, ids=lambda dead: dead.id)
def test_legacy_dead_symbol_is_named_only_in_dead_code(dead: Any) -> None:
    path, _, qualified = dead.symbol.partition("::")
    live = _without_dead_code(_sources("head"))
    name = qualified.rpartition(".")[2] or Path(path).stem
    stem = Path(path).stem
    assert name.isidentifier()
    if any(re.search(rf"^def {name}\(", text, re.MULTILINE) for text in live.values()):
        # A copied helper: the other copies keep the name alive, so look at this file and at
        # whatever could reach into it.
        assert not _places(name, {path: live[path]})
        reach = re.compile(rf"\b{stem}\.{name}\b|\b{stem} import .*\b{name}\b")
        assert not [
            line for text in live.values() for line in text.splitlines() if reach.search(line)
        ]
    else:
        assert not _places(name, live)


def test_legacy_dead_through_dead_is_called_from_dead_code() -> None:
    assert _places("_old_vat_table", _sources("head"))
    assert not _places("_old_vat_table", _without_dead_code(_sources("head")))


@pytest.mark.parametrize("decoy", _legacy().decoys, ids=lambda decoy: decoy.id)
def test_legacy_decoy_is_named_where_the_truth_says(decoy: Any) -> None:
    name = decoy.symbol.rpartition("::")[2]
    home = decoy.evidence.symbol.partition("::")[0]
    live = _without_dead_code(_sources("head"))
    assert decoy.evidence.snippet in live[home]
    places = _places(name, live)
    in_package = {path for path, _ in places if path.startswith("legacy_billing/")}
    expected: dict[str, Callable[[], bool]] = {
        "test_only": lambda: bool(places) and not in_package,
        "dispatch_table": lambda: places == [(home, decoy.evidence.snippet)],
        "reexport": lambda: in_package == {"legacy_billing/__init__.py"},
        "dynamic_lookup": lambda: not places,
        "callback": lambda: (
            (home, decoy.evidence.snippet) in places and {path for path, _ in places} == {home}
        ),
    }
    assert expected[decoy.kind](), places


def test_legacy_has_no_other_dead_code() -> None:
    """What the package defines and no other line names is listed, as dead code or as a decoy."""
    live = _without_dead_code(_sources("head"))
    listed = {decoy.symbol for decoy in _legacy().decoys}
    unnamed = {
        f"{path}::{name}"
        for path, text in live.items()
        if path.startswith("legacy_billing/")
        for name in re.findall(r"^ *(?:def|class) (\w+)", text, re.MULTILINE)
        if not name.startswith("__") and not _places(name, live)
    }
    assert unnamed <= listed


def test_legacy_dynamic_calls_reach_the_dispatch_decoys() -> None:
    truth = _legacy()
    reached = {target for call in truth.dynamic_calls for target in call.targets}
    kinds = {"dispatch_table", "dynamic_lookup", "callback"}
    assert reached == {decoy.symbol for decoy in truth.decoys if decoy.kind in kinds}
