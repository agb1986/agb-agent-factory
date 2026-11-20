"""Tests for evals/doc_drift.py and the `doc_drift` expectation of the sandbox_tests grader.

No Docker, no API key and no network: the drift check reads files and git, and runs nothing. Each
docs fixture's head must fail the check, and a copy of it fixed by hand, the way the documentation
agent is asked to, must pass it.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.cases import ExpectedDocDrift, ExpectedRun
from evals.doc_drift import (
    bare_references,
    check_doc_drift,
    missing_docstrings,
    named_in,
    top_level_names,
)
from evals.graders.sandbox_tests import changed_files, check_run
from fixtures.build import build

DOCS = ("README.md", "docs/**/*.md")


# --- the helpers


def test_missing_docstrings_lists_public_functions_classes_and_methods() -> None:
    source = (
        '"""Module."""\n'
        "def f(): ...\n"
        'def g():\n    """Doc."""\n'
        "def _h(): ...\n"
        "class C:\n"
        "    def __init__(self): ...\n"
        "    def m(self): ...\n"
        "    def _p(self): ...\n"
        "class _Hidden:\n    def m(self): ...\n"
        'class D:\n    """Doc."""\n    async def n(self): ...\n'
    )
    assert missing_docstrings(source) == ["f", "C", "C.m", "D.n"]


def test_top_level_names_are_public_defs_and_assignments() -> None:
    source = "A = 1\n_B = 2\nC: int = 3\nx, y = 1, 2\ndef f(): ...\nclass K: ...\nimport os\n"
    assert top_level_names(source) == {"A", "C", "f", "K"}


def test_named_in_reads_identifiers_inside_backticks_only() -> None:
    text = "Call `config.load(path)` or `slugify()`; load is prose.\n```\ncode\n```"
    assert named_in(text) >= {"config", "load", "path", "slugify"}
    assert "prose" not in named_in(text)


def test_bare_references_are_lone_identifiers_with_or_without_call_parens() -> None:
    text = "`load`, `load()`, ` read `, `config.load`, `load(path)`, `a b`"
    assert bare_references(text) == {"load", "read"}


@pytest.mark.parametrize("helper", [missing_docstrings, top_level_names])
def test_the_source_helpers_raise_on_source_that_does_not_parse(
    helper: Callable[[str], object],
) -> None:
    with pytest.raises(SyntaxError):
        helper("def f(:\n")


# --- the case field


def test_expected_doc_drift_needs_unique_python_paths_and_some_docs() -> None:
    drift = ExpectedDocDrift(paths=("a.py",), docs=("README.md",))
    assert ExpectedRun(doc_drift=drift).doc_drift == drift
    for paths, docs in [
        ((), DOCS),
        (("a.py", "a.py"), DOCS),
        (("README.md",), DOCS),
        (("a.py",), ()),
        (("a.py",), ("",)),
    ]:
        with pytest.raises(ValidationError):
            ExpectedDocDrift(paths=paths, docs=docs)


# --- the fixtures, as they are and fixed by hand


def _repo(tmp_path: Path, name: str) -> Path:
    return build([name], dest=tmp_path / "repos")[0].path


def _drift(repo: Path, paths: tuple[str, ...]) -> list[str]:
    return check_doc_drift(repo, paths, DOCS, changed_files(repo))


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1))


def test_new_function_head_drifts_until_slugify_is_documented(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-new-function")
    assert _drift(repo, ("textkit/words.py",)) == [
        "doc_drift: textkit/words.py has no docstring for slugify",
        "doc_drift: no docs file names slugify from textkit/words.py",
    ]
    _replace(
        repo / "textkit" / "words.py",
        "    found = words(text)",
        '    """Join the words of `text` with `sep`; raise ValueError when there are none."""\n'
        "    found = words(text)",
    )
    (repo / "README.md").write_text(
        (repo / "README.md").read_text() + "\n`slugify(text, sep)` joins them with `sep`.\n"
    )
    assert _drift(repo, ("textkit/words.py",)) == []


def test_new_class_head_drifts_until_shelf_is_documented(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-new-class")
    assert _drift(repo, ("shelf/books.py",)) == [
        "doc_drift: shelf/books.py has no docstring for Shelf, Shelf.add, Shelf.find",
        "doc_drift: no docs file names Shelf from shelf/books.py",
    ]
    books = repo / "shelf" / "books.py"
    _replace(books, "class Shelf:\n", 'class Shelf:\n    """Titles by ISBN."""\n\n')
    _replace(books, "        if not isbn_ok", '        """Add a title."""\n        if not isbn_ok')
    _replace(
        books,
        "        return self._titles",
        '        """Find a title."""\n        return self._titles',
    )
    (repo / "docs" / "api.md").write_text(
        (repo / "docs" / "api.md").read_text() + "\n`Shelf` holds titles by ISBN.\n"
    )
    assert _drift(repo, ("shelf/books.py",)) == []


def test_renamed_function_head_drifts_until_load_is_gone_from_the_docs(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-renamed-function")
    assert _drift(repo, ("settings/loader.py",)) == [
        "doc_drift: no docs file names read_config from settings/loader.py",
        "doc_drift: the docs still name load, gone from the code",
    ]
    _replace(repo / "README.md", "Call `load`", "Call `read_config`")
    _replace(repo / "docs" / "format.md", "`load()`", "`read_config()`")
    assert _drift(repo, ("settings/loader.py",)) == []


def test_new_module_head_drifts_until_units_mass_is_documented(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-new-module")
    paths = ("units/mass.py", "units/length.py")
    assert _drift(repo, paths) == [
        "doc_drift: units/mass.py has no docstring for kg_to_lb, lb_to_kg",
        "doc_drift: no docs file names POUNDS_PER_KILOGRAM, kg_to_lb, lb_to_kg from units/mass.py",
    ]
    mass = repo / "units" / "mass.py"
    mass.write_text(
        '"""Masses."""\n\n'
        + mass.read_text()
        .replace("    if kilograms", '    """Kilograms to pounds."""\n    if kilograms')
        .replace("    if pounds", '    """Pounds to kilograms."""\n    if pounds')
    )
    (repo / "README.md").write_text(
        (repo / "README.md").read_text()
        + "\n## Mass (`units.mass`)\n\n- `kg_to_lb` and `lb_to_kg`, using `POUNDS_PER_KILOGRAM`.\n"
    )
    assert _drift(repo, paths) == []


def test_a_docs_file_the_agent_creates_counts(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-renamed-function")
    _replace(repo / "README.md", "Call `load`", "Call the reader")
    _replace(repo / "docs" / "format.md", "`load()`", "it")
    (repo / "docs" / "api").mkdir()
    (repo / "docs" / "api" / "loader.md").write_text("# loader\n\n`read_config(path)`\n")
    assert _drift(repo, ("settings/loader.py",)) == []
    assert check_doc_drift(repo, ("settings/loader.py",), ("README.md",), []) == [
        "doc_drift: no docs file names read_config from settings/loader.py"
    ]


# --- what the check reports instead of raising


def test_code_changes_are_reported_by_the_docs_check_rules(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-renamed-function")
    _replace(repo / "README.md", "Call `load`", "Call `read_config`")
    _replace(repo / "docs" / "format.md", "`load()`", "`read_config()`")
    _replace(repo / "settings" / "loader.py", "return pairs", "return dict(pairs)")
    (repo / "settings" / "extra.py").write_text('"""New."""\n')
    (repo / "docs" / "format.md").rename(repo / "docs" / "file-format.md")
    failures = _drift(repo, ("settings/loader.py",))
    assert "doc_drift: settings/loader.py changed outside its docstrings and comments" in failures
    assert "doc_drift: the docs round created the source file settings/extra.py" in failures
    assert "doc_drift: the docs round deleted docs/format.md" in failures


def test_missing_outside_and_unparsable_paths_are_reported_not_raised(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-new-function")
    (repo / "textkit" / "words.py").write_text("def broken(:\n")
    failures = check_doc_drift(
        repo, ("textkit/words.py", "textkit/nope.py", "../outside.py"), DOCS, []
    )
    assert failures[0] == "doc_drift: textkit/words.py does not parse (line 1)"
    assert failures[1] == "doc_drift: textkit/nope.py is not a file in the work tree"
    assert failures[2].startswith("doc_drift: ../outside.py: ")


# --- the grader


def test_check_run_reports_drift_with_the_other_change_checks(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs-new-function")
    run = ExpectedRun(
        tests="unchanged",
        doc_drift=ExpectedDocDrift(paths=("textkit/words.py",), docs=DOCS),
    )
    assert check_run(run, {}, repo) == [
        "doc_drift: textkit/words.py has no docstring for slugify",
        "doc_drift: no docs file names slugify from textkit/words.py",
    ]
    (repo / "tests" / "test_words.py").write_text("")
    failures = check_run(run, {}, repo)
    assert failures[0] == "test files changed, expected none: tests/test_words.py"
    assert "doc_drift: tests/test_words.py changed outside its docstrings and comments" in failures
