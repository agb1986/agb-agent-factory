"""Tests of `deps.dead_code` on the `legacy-billing` fixture and on small trees built per test."""

from pathlib import Path

import pytest
from repo_helpers import run_git

from fixtures import build as fixture_build
from tools import _dead_code, deps, registry
from tools._common import PathEscapeError, ToolNotFoundError
from tools._dead_code import CONFIDENCE, DYNAMIC_CONFIDENCE, EXPORT_CONFIDENCE, Report

LEGACY = fixture_build.discover()["legacy-billing"]
DEFECTS = {d.id: d for d in LEGACY.load_manifest().defects if d.category == "dead-code"}
TRUTH = LEGACY.load_ground_truth()
assert TRUTH is not None
DEAD = {d.id: d for d in TRUTH.dead_code}
DECOYS = {d.id: d for d in TRUTH.decoys}


@pytest.fixture(scope="module")
def legacy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the built `legacy-billing` repository, its head revision checked out."""
    dest = tmp_path_factory.mktemp("legacy") / "repos"
    return fixture_build.build(["legacy-billing"], dest=dest)[0].path


@pytest.fixture(scope="module")
def legacy_report(legacy: Path) -> Report:
    """Return the report of the `legacy-billing` repository."""
    return _dead_code.analyse(legacy)


def make(root: Path, files: dict[str, str]) -> Path:
    """Write `files` under `root` as a git repository with every file tracked, and return it."""
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    run_git(root, "init", "-q")
    run_git(root, "add", "-A")
    return root


def analyse(root: Path, files: dict[str, str]) -> Report:
    """Return the report of a repository made of `files`."""
    return _dead_code.analyse(make(root, files))


def rules(findings: list[_dead_code.Finding]) -> dict[str, str]:
    """Return the rule of each finding by its symbol."""
    return {f.symbol: f.rule for f in findings}


# legacy-billing


def test_the_fixture_has_seven_dead_entries_and_nine_decoys() -> None:
    assert set(DEFECTS) == set(DEAD)
    assert len(DEFECTS) == 7
    assert len(DECOYS) == 9


@pytest.mark.parametrize("defect_id", sorted(DEFECTS))
def test_legacy_billing_dead_entry_is_found(legacy_report: Report, defect_id: str) -> None:
    defect, truth = DEFECTS[defect_id], DEAD[defect_id]
    found = [f for f in legacy_report.dead if f.symbol == truth.symbol]
    assert len(found) == 1
    assert found[0].path == defect.file
    assert defect.line_start <= found[0].line <= defect.line_end
    assert found[0].kind == truth.kind


def test_legacy_billing_dead_list_holds_nothing_else(legacy_report: Report) -> None:
    assert sorted(f.symbol for f in legacy_report.dead) == sorted(d.symbol for d in DEAD.values())


def test_legacy_billing_findings_name_their_rule(legacy_report: Report) -> None:
    found = rules(legacy_report.dead)
    by_id = {defect_id: found[truth.symbol] for defect_id, truth in DEAD.items()}
    assert by_id == {
        "dead-function": "vulture",
        "dead-class": "vulture",
        "dead-method": "vulture",
        "dead-module": "module",
        "dead-vat-rate": "vulture",
        "dead-through-dead": "through_dead",
        "dead-copy": "private",
    }
    assert all(f.confidence == CONFIDENCE[f.rule] for f in legacy_report.dead)


@pytest.mark.parametrize("decoy_id", sorted(DECOYS))
def test_legacy_billing_decoy_is_not_called_dead(legacy_report: Report, decoy_id: str) -> None:
    assert DECOYS[decoy_id].symbol not in rules(legacy_report.dead)


@pytest.mark.parametrize("decoy_id", sorted(d for d in DECOYS if d.startswith("decoy-export-")))
def test_legacy_billing_exporter_names_the_lookup(legacy_report: Report, decoy_id: str) -> None:
    decoy = DECOYS[decoy_id]
    found = [f for f in legacy_report.dynamic if f.symbol == decoy.symbol]
    assert len(found) == 1
    assert found[0].confidence == DYNAMIC_CONFIDENCE
    assert decoy.evidence.symbol in found[0].detail
    assert decoy.evidence.symbol.endswith("::export_ledger")


def test_legacy_billing_dynamic_list_holds_the_exporters_only(legacy_report: Report) -> None:
    exporters = {d.symbol for i, d in DECOYS.items() if i.startswith("decoy-export-")}
    assert len(exporters) == 2
    assert set(rules(legacy_report.dynamic)) == exporters
    assert legacy_report.skipped == {}


def test_legacy_billing_text(legacy: Path) -> None:
    text = deps.dead_code(legacy, max_bytes=100_000)
    lines = text.splitlines()
    assert lines[0].startswith("dead code: 7 dead, 2 a dynamic lookup may reach, 0 unused exports;")
    assert lines[1] == "dead:"
    assert "not called dead, a dynamic lookup may reach it:" in lines
    row = next(line for line in lines if "taxes.py::_old_vat_table" in line)
    assert row.startswith("legacy_billing/taxes.py:20: ")
    assert "(function, confidence 60, through_dead)" in row
    assert "legacy_billing/taxes.py::old_vat_rate" in row


def test_the_run_changes_nothing_in_the_repository(legacy: Path) -> None:
    before = sorted(p for p in legacy.rglob("*") if ".git" not in p.parts)
    deps.dead_code(legacy)
    assert sorted(p for p in legacy.rglob("*") if ".git" not in p.parts) == before
    assert run_git(legacy, "status", "--porcelain") == ""


# Python rules


def test_dead_through_dead_repeats_until_nothing_new(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "main.py": (
                "def a():\n    b()\n\n\n"
                "def b():\n    c()\n\n\n"
                "def c():\n    used()\n\n\n"
                "def used():\n    pass\n\n\n"
                "if __name__ == '__main__':\n    used()\n"
            )
        },
    )
    assert rules(report.dead) == {
        "main.py::a": "vulture",
        "main.py::b": "through_dead",
        "main.py::c": "through_dead",
    }
    assert "main.py::b" in next(f.detail for f in report.dead if f.symbol == "main.py::c")


def test_private_copy_is_dead_though_another_module_uses_the_name(tmp_path: Path) -> None:
    body = "def _pad(x):\n    return x\n\n\n"
    report = analyse(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/one.py": body + "def go():\n    return _pad(1)\n",
            "pkg/two.py": body + "def run():\n    return 2\n",
            "pkg/__main__.py": "from pkg import one, two\n\none.go()\ntwo.run()\n",
        },
    )
    assert rules(report.dead) == {"pkg/two.py::_pad": "private"}
    assert report.dead[0].confidence == 80


@pytest.mark.parametrize(
    "use",
    [
        "from pkg.two import _pad\n\nprint(_pad)\n",
        "from pkg import two\n\nprint(two._pad)\n",
        "import pkg.two\n\nprint(pkg.two._pad)\n",
    ],
)
def test_private_name_used_from_another_module_is_not_dead(tmp_path: Path, use: str) -> None:
    report = analyse(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/one.py": "def _pad(x):\n    return x\n\n\nprint(_pad(1))\n",
            "pkg/two.py": "def _pad(x):\n    return x\n",
            "pkg/__main__.py": "import pkg.one\n" + use,
        },
    )
    assert "pkg/two.py::_pad" not in rules(report.dead)


def test_a_star_import_reaches_a_private_name_only_through_all(tmp_path: Path) -> None:
    files = {
        "one.py": "def _pad(x):\n    return x\n\n\nprint(_pad(1))\n",
        "two.py": "def _pad(x):\n    return x\n",
        "main.py": "import one\nfrom two import *\n\nif __name__ == '__main__':\n    print(one)\n",
    }
    root = make(tmp_path, files)
    assert rules(_dead_code.analyse(root).dead) == {"two.py::_pad": "private"}
    (root / "two.py").write_text("__all__ = ['_pad']\n" + files["two.py"], encoding="utf-8")
    assert _dead_code.analyse(root).dead == []


def test_unimported_module_is_reported_once_and_entry_points_are_not(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/__main__.py": "from pkg import live\n\nlive.go()\n",
            "pkg/live.py": "def go():\n    pass\n",
            "pkg/old.py": "def first():\n    pass\n\n\ndef second():\n    first()\n",
            "pkg/script.py": "if __name__ == '__main__':\n    print(1)\n",
            "setup.py": "print('setup')\n",
            "conftest.py": "print('conftest')\n",
            "tests/test_live.py": "from pkg import live\n\n\ndef test_go():\n    live.go()\n",
        },
    )
    assert rules(report.dead) == {"pkg/old.py": "module"}
    assert (report.dead[0].line, report.dead[0].kind) == (1, "module")


def test_module_only_a_dead_module_imports_is_dead_too(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/old.py": "from pkg import older\n\nolder.go()\n",
            "pkg/older.py": "def go():\n    pass\n",
        },
    )
    assert rules(report.dead) == {"pkg/old.py": "module", "pkg/older.py": "through_dead"}


def test_module_named_in_pyproject_is_an_entry_point(tmp_path: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/tool.py": "def main():\n    pass\n"}
    script = '[project]\nname = "x"\n\n[project.scripts]\nx = "pkg.tool:main"\n'
    report = analyse(tmp_path, {**files, "pyproject.toml": script})
    assert report.dead == []


def test_a_function_called_only_from_a_test_is_not_dead(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "lib.py": "def only_tested():\n    return 1\n",
            "test_lib.py": "import lib\n\n\ndef test_it():\n    assert lib.only_tested() == 1\n",
        },
    )
    assert report.dead == []
    assert report.dynamic == []


@pytest.mark.parametrize(
    "init",
    ["from .impl import shown\n", "from .impl import shown as shown\n", "from .impl import *\n"],
)
def test_a_name_an_init_reexports_is_not_dead(tmp_path: Path, init: str) -> None:
    report = analyse(
        tmp_path,
        {
            "pkg/__init__.py": init,
            "pkg/impl.py": "def shown():\n    pass\n",
            "main.py": "import pkg\n\nif __name__ == '__main__':\n    print(pkg)\n",
        },
    )
    assert report.dead == []


def test_a_decorated_definition_is_reported_at_its_def_line(tmp_path: Path) -> None:
    text = (
        "from functools import cache\n\n\n@cache\ndef dead():\n    return 1\n\n\n"
        "if __name__ == '__main__':\n    print(1)\n"
    )
    report = analyse(tmp_path, {"main.py": text})
    assert [(f.symbol, f.line) for f in report.dead] == [("main.py::dead", 5)]


# Dynamic lookups


@pytest.mark.parametrize(
    ("caller", "where"),
    [
        ("import jobs\n\n\ndef go(n):\n    return getattr(jobs, 'run_' + n)()\n", "main.py:5"),
        ("import jobs\n\n\ndef go(n):\n    return getattr(jobs, n)()\n", "main.py:5"),
        ("import jobs\n\n\ndef go(n):\n    return vars(jobs)[n]()\n", "main.py:5"),
        ("import jobs\n\n\ndef go(n):\n    return jobs.__dict__[n]()\n", "main.py:5"),
    ],
)
def test_a_run_time_name_keeps_a_function_out_of_the_dead_list(
    tmp_path: Path, caller: str, where: str
) -> None:
    files = {
        "jobs.py": "def run_nightly():\n    pass\n",
        "main.py": caller + "\n\nif __name__ == '__main__':\n    go('nightly')\n",
    }
    report = analyse(tmp_path, files)
    assert report.dead == []
    assert [f.symbol for f in report.dynamic] == ["jobs.py::run_nightly"]
    assert report.dynamic[0].detail.startswith(where + " ")
    assert "main.py::go" in report.dynamic[0].detail


def test_a_lookup_by_prefix_reaches_only_names_with_that_prefix(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "jobs.py": "def run_nightly():\n    pass\n\n\ndef stop():\n    pass\n",
            "other.py": "def run_else():\n    pass\n",
            "main.py": (
                "import jobs\nimport other\n\n\n"
                "def go(n):\n    return getattr(jobs, f'run_{n}')()\n\n\n"
                "if __name__ == '__main__':\n    go('nightly')\n    print(other)\n"
            ),
        },
    )
    assert set(rules(report.dead)) == {"jobs.py::stop", "other.py::run_else"}
    assert set(rules(report.dynamic)) == {"jobs.py::run_nightly"}


@pytest.mark.parametrize(
    "use",
    ["TABLE = {'a': handler}\nprint(TABLE['a']())\n", "HOOKS = []\nHOOKS.append(handler)\n"],
)
def test_a_function_stored_as_a_value_is_not_dead(tmp_path: Path, use: str) -> None:
    text = f"def handler():\n    pass\n\n\n{use}\nif __name__ == '__main__':\n    print(1)\n"
    assert analyse(tmp_path, {"main.py": text}).dead == []


def test_a_registering_decorator_and_an_unknown_base_are_dynamic(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "main.py": (
                "from web import app, Base\n\n\n"
                "@app.get('/')\ndef home():\n    pass\n\n\n"
                "class Plugin(Base):\n    pass\n\n\n"
                "class Plain:\n    pass\n\n\n"
                "if __name__ == '__main__':\n    print(1)\n"
            )
        },
    )
    assert set(rules(report.dynamic)) == {"main.py::home", "main.py::Plugin"}
    assert set(rules(report.dead)) == {"main.py::Plain"}


def test_a_module_imported_by_a_run_time_name_is_dynamic(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "plugins/__init__.py": "",
            "plugins/one.py": "print(1)\n",
            "far.py": "print(2)\n",
            "main.py": (
                "import importlib\n\n\n"
                "def load(n):\n    return importlib.import_module('plugins.' + n)\n\n\n"
                "if __name__ == '__main__':\n    load('one')\n"
            ),
        },
    )
    assert rules(report.dynamic) == {"plugins/one.py": "module"}
    assert report.dynamic[0].detail.startswith("main.py:5 ")
    assert rules(report.dead) == {"far.py": "module"}


def test_what_a_dynamic_candidate_calls_is_not_dead(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "jobs.py": "def run_it():\n    helper()\n\n\ndef helper():\n    pass\n",
            "main.py": (
                "import jobs\n\nif __name__ == '__main__':\n    getattr(jobs, input())()\n"
            ),
        },
    )
    assert report.dead == []


# Nothing in the repository configures the run

PLAIN = {
    "main.py": "def unused():\n    pass\n\n\nif __name__ == '__main__':\n    print(1)\n",
}


@pytest.mark.parametrize(
    "extra",
    [
        {"pyproject.toml": '[tool.vulture]\nignore_names = ["unused"]\nexclude = ["main.py"]\n'},
        {"whitelist.py": "unused  # unused function (main.py:1)\n"},
        {"vulture_whitelist.py": "from main import unused\nunused\n"},
        {"main.py": PLAIN["main.py"].replace("unused():", "unused():  # noqa")},
        {"main.py": PLAIN["main.py"].replace("unused():", "unused():  # noqa: V103")},
        {"main.py": "# vulture: ignore\n" + PLAIN["main.py"]},
        {"pyproject.toml": "not toml ["},
    ],
)
def test_configuration_in_the_repository_silences_nothing(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    report = analyse(tmp_path, {**PLAIN, **extra})
    assert rules(report.dead) == {"main.py::unused": "vulture"}


def test_a_whitelist_file_is_listed_as_not_scanned(tmp_path: Path) -> None:
    root = make(tmp_path, {**PLAIN, "whitelist.py": "unused  # unused function (main.py:1)\n"})
    assert "not scanned: whitelist.py (whitelist)" in deps.dead_code(root).splitlines()


def test_nothing_read_is_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    root = tmp_path / "repo"
    root.mkdir()
    text = f"from pathlib import Path\n\nPath({str(marker)!r}).write_text('x')\n"
    analyse(root, {"main.py": text, "conftest.py": text, "setup.py": text})
    assert not marker.exists()


# Files that cannot be read, the cap and the path


def test_a_syntax_error_is_skipped_and_the_rest_is_read(tmp_path: Path) -> None:
    root = make(tmp_path, {**PLAIN, "bad.py": "def (:\n"})
    lines = deps.dead_code(root).splitlines()
    assert "not scanned: bad.py (syntax_error)" in lines
    assert any("main.py::unused" in line for line in lines)


def test_an_oversized_file_is_skipped(tmp_path: Path) -> None:
    root = make(tmp_path, {**PLAIN, "big.py": "x = 1\n" + "#" * deps.MAX_FILE_BYTES})
    assert "not scanned: big.py (too_large)" in deps.dead_code(root).splitlines()


def test_a_deeply_nested_file_is_skipped(tmp_path: Path) -> None:
    text = "x = 1" + " + 1" * (2 * _dead_code.MAX_DEPTH) + "\n"
    root = make(tmp_path, {**PLAIN, "deep.py": text})
    lines = deps.dead_code(root).splitlines()
    assert "not scanned: deep.py (too_deep)" in lines
    assert any("main.py::unused" in line for line in lines)


def test_a_symlink_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def secret_name():\n    pass\n", encoding="utf-8")
    (root / "link.py").symlink_to(outside)
    text = deps.dead_code(make(root, PLAIN))
    assert "not scanned: link.py (symlink)" in text.splitlines()
    assert "secret_name" not in text


def test_an_untracked_file_is_not_read(tmp_path: Path) -> None:
    root = make(tmp_path, {**PLAIN, ".gitignore": "build/\n"})
    (root / "build").mkdir()
    (root / "build" / "out.py").write_text("def built():\n    pass\n", encoding="utf-8")
    assert "build/out.py" not in deps.dead_code(root)


def test_the_cap_truncates(tmp_path: Path) -> None:
    body = "".join(f"def unused_{n:03d}():\n    pass\n\n\n" for n in range(200))
    root = make(tmp_path, {"main.py": body + "if __name__ == '__main__':\n    print(1)\n"})
    whole = deps.dead_code(root, max_bytes=10**6)
    short = deps.dead_code(root, max_bytes=500)
    assert "dead code: 200 dead" in short
    assert "truncated" in short
    assert len(short) < 800 < len(whole)
    assert "truncated" not in whole


def test_path_limits_what_is_listed_not_what_is_read(tmp_path: Path) -> None:
    root = make(
        tmp_path,
        {
            "a/__init__.py": "",
            "a/one.py": "def used():\n    pass\n\n\ndef dead_a():\n    pass\n",
            "b/__init__.py": "",
            "b/two.py": "from a.one import used\n\nused()\n\n\ndef dead_b():\n    pass\n",
            "main.py": "import b.two\n\nif __name__ == '__main__':\n    print(b)\n",
        },
    )
    assert set(rules(_dead_code.analyse(root, "a").dead)) == {"a/one.py::dead_a"}
    assert set(rules(_dead_code.analyse(root, "b/two.py").dead)) == {"b/two.py::dead_b"}
    assert len(_dead_code.analyse(root, ".").dead) == 2


@pytest.mark.parametrize("path", ["../elsewhere", "/etc", "a/../../x"])
def test_a_path_outside_the_root_is_an_input_error(tmp_path: Path, path: str) -> None:
    root = make(tmp_path, PLAIN)
    with pytest.raises(PathEscapeError):
        deps.dead_code(root, path)


def test_a_missing_path_is_not_found(tmp_path: Path) -> None:
    root = make(tmp_path, PLAIN)
    with pytest.raises(ToolNotFoundError):
        deps.dead_code(root, "gone.py")


def test_findings_are_sorted_and_the_same_on_every_run(tmp_path: Path) -> None:
    body = (
        "def z():\n    pass\n\n\ndef y():\n    pass\n\n\nif __name__ == '__main__':\n    print(1)\n"
    )
    root = make(tmp_path, {"b.py": body.replace("z", "q").replace("y", "r"), "a.py": body})
    report = _dead_code.analyse(root)
    assert [(f.path, f.line) for f in report.dead] == [("a.py", 1), ("a.py", 5), ("b.py", 1), ("b.py", 5)]  # fmt: skip
    assert deps.dead_code(root) == deps.dead_code(root)


def test_an_empty_repository_reports_nothing(tmp_path: Path) -> None:
    text = deps.dead_code(make(tmp_path, {"README.md": "nothing\n"}))
    assert text == (
        "dead code: 0 dead, 0 a dynamic lookup may reach, 0 unused exports; "
        "0 Python and 0 TypeScript or JavaScript files read\n"
    )


# TypeScript and JavaScript


def test_unused_exports_are_listed_apart(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "src/a.ts": (
                "export function used() {}\n"
                "export function unused() {}\n"
                "export const alsoUnused = 1;\n"
                "export type Shape = { x: number };\n"
                "// export const commented = 1;\n"
                "function local() {}\n"
            ),
            "src/b.ts": "import { used } from './a';\nused();\n",
        },
    )
    assert report.dead == []
    assert {(f.symbol, f.line) for f in report.exports} == {
        ("src/a.ts::unused", 2),
        ("src/a.ts::alsoUnused", 3),
        ("src/a.ts::Shape", 4),
    }
    assert {(f.rule, f.confidence) for f in report.exports} == {("export", EXPORT_CONFIDENCE)}


def test_export_lists_defaults_and_aliases(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "src/a.ts": "const x = 1;\nconst y = 2;\nexport { x, y as why };\nexport default x;\n",
            "src/b.ts": "import { x as renamed } from './a';\nconsole.log(renamed);\n",
            "src/c.ts": "export default function main() {}\n",
            "src/d.ts": "import main from './c';\nmain();\n",
        },
    )
    assert {f.symbol for f in report.exports} == {"src/a.ts::why", "src/a.ts::default"}


@pytest.mark.parametrize(
    "use",
    [
        "import * as a from './a';\nconsole.log(a);\n",
        "const a = require('./a');\n",
        "export * from './a';\n",
        "export { f } from './a';\n",
        "const a = await import('./a');\n",
        "import type { f } from './a';\n",
        "import d, { f } from './a';\n",
    ],
)
def test_each_form_of_import_is_a_use(tmp_path: Path, use: str) -> None:
    files = {"src/a.ts": "export function f() {}\n", "src/b.ts": use}
    assert analyse(tmp_path, files).exports == []


def test_a_barrel_and_entry_files_are_not_reported(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "src/index.ts": "export { f } from './a';\nexport const shown = 1;\n",
            "src/a.ts": "export function f() {}\n",
            "src/types.d.ts": "export type T = number;\n",
            "vite.config.ts": "export default {};\n",
            "src/a.test.ts": "export const helper = 1;\n",
        },
    )
    assert report.exports == []


def test_a_side_effect_import_uses_no_name(tmp_path: Path) -> None:
    report = analyse(tmp_path, {"src/a.ts": "export const f = 1;\n", "src/b.ts": "import './a';\n"})
    assert {f.symbol for f in report.exports} == {"src/a.ts::f"}


def test_an_import_by_a_run_time_path_is_dynamic(tmp_path: Path) -> None:
    report = analyse(
        tmp_path,
        {
            "src/pages/a.ts": "export const page = 1;\n",
            "src/other.ts": "export const lone = 1;\n",
            "src/load.js": "const n = name();\nimport(`./pages/${n}`);\n",
        },
    )
    assert {f.symbol for f in report.exports} == {"src/other.ts::lone"}
    assert {f.symbol for f in report.dynamic} == {"src/pages/a.ts::page"}
    assert report.dynamic[0].detail.startswith("src/load.js:2")


def test_unused_exports_are_rendered_in_their_own_section(tmp_path: Path) -> None:
    lines = deps.dead_code(make(tmp_path, {"a.ts": "export const f = 1;\n"})).splitlines()
    assert lines[0].endswith("0 Python and 1 TypeScript or JavaScript files read")
    assert lines[1] == "exports no other file imports:"
    assert lines[2].startswith("a.ts:1: a.ts::f (export, confidence 40, export)")


# The declaration


def test_the_tool_is_declared_read_only_with_a_closed_schema() -> None:
    spec = next(s for s in deps.TOOLS if s.name == "deps.dead_code")
    assert spec.impl is deps.dead_code
    assert (spec.writes, spec.reaches_out, spec.outside_write) == (False, False, False)
    assert spec.input_schema["additionalProperties"] is False
    assert set(spec.input_schema["properties"]) == {"path"}
    assert all(p["description"] for p in spec.input_schema["properties"].values())
    assert "dynamic" in spec.description
    assert registry.impl_for("deps.dead_code") is deps.dead_code
