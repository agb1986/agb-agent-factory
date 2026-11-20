"""The fixture repositories build reproducibly and their defect manifests match the head trees."""

import json
import os
import subprocess
import sys
import tarfile
import time
from io import BytesIO
from pathlib import Path

import pytest

from factory.exit import ExitCode
from fixtures import build as fixture_build
from fixtures.schema import (
    DefectManifest,
    FixtureManifestError,
    check_against_tree,
    load_manifest,
)

REQUIRED = {
    "off-by-one",
    "clean-refactor",
    "untested-change",
    "mixed",
    "docs-only",
    "test-only",
    "sql-injection",
    "n-plus-one",
    "off-by-one-single-file",
    "quadratic-loop",
    "missing-index-hint",
    "hardcoded-secret",
    "path-traversal",
    "secret-in-config",
    "sast-false-positive",
    "crash-empty-average",
    "crash-missing-config-key",
    "crash-loader-caller",
    "crash-first-of-empty",
    "refactor-duplicate",
    "refactor-deprecated",
    "refactor-magic-numbers",
    "docs-new-function",
    "docs-new-class",
    "docs-renamed-function",
    "docs-new-module",
}
CLEAN = {
    "clean-refactor",
    "docs-only",
    "test-only",
    "untested-change-covered",
    "sast-false-positive",
}
FAILS_ON_HEAD = {"off-by-one-red", "mixed-red", "sql-injection-red"}
"""Fixtures built for the fixer: one test fails on `head` on purpose, and passes on `base`."""
FIXTURES = fixture_build.discover()


def git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return stdout."""
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, encoding="utf-8"
    )
    return done.stdout


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, fixture_build.BuiltFixture]:
    """Every fixture, built once into a temporary directory."""
    dest = tmp_path_factory.mktemp("repos")
    return {b.name: b for b in fixture_build.build(dest=dest)}


def test_every_required_fixture_exists() -> None:
    assert REQUIRED <= set(FIXTURES)


def test_rebuild_gives_identical_shas(
    built: dict[str, fixture_build.BuiltFixture], tmp_path: Path
) -> None:
    again = {b.name: b for b in fixture_build.build(dest=tmp_path)}
    assert {n: (b.base_sha, b.head_sha) for n, b in again.items()} == {
        n: (b.base_sha, b.head_sha) for n, b in built.items()
    }


def test_rebuild_in_place_wipes_old_files(tmp_path: Path) -> None:
    (first,) = fixture_build.build(["docs-only"], dest=tmp_path)
    stray = first.path / "stray.txt"
    stray.write_text("left over")
    (second,) = fixture_build.build(["docs-only"], dest=tmp_path)
    assert not stray.exists()
    assert second.head_sha == first.head_sha


def test_build_is_fast(tmp_path: Path) -> None:
    start = time.monotonic()
    fixture_build.build(dest=tmp_path)
    assert time.monotonic() - start < 10


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_repo_has_base_and_head_refs(
    name: str, built: dict[str, fixture_build.BuiltFixture]
) -> None:
    repo = built[name]
    assert git(repo.path, "rev-parse", "refs/heads/base").strip() == repo.base_sha
    assert git(repo.path, "rev-parse", "refs/heads/head").strip() == repo.head_sha
    assert git(repo.path, "rev-parse", "head~1").strip() == repo.base_sha
    assert git(repo.path, "status", "--porcelain") == ""
    assert git(repo.path, "diff", "--name-only", "base", "head").strip()


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_commits_have_fixed_identity_and_dates(
    name: str, built: dict[str, fixture_build.BuiltFixture]
) -> None:
    log = git(built[name].path, "log", "--format=%an|%ae|%cn|%ce|%aI|%cI", "head").splitlines()
    ident = f"{fixture_build.AUTHOR_NAME}|{fixture_build.AUTHOR_EMAIL}"
    assert log == [
        f"{ident}|{ident}|2026-01-05T10:00:00Z|2026-01-05T10:00:00Z",
        f"{ident}|{ident}|2026-01-05T09:00:00Z|2026-01-05T09:00:00Z",
    ]


def test_repo_has_no_hooks(built: dict[str, fixture_build.BuiltFixture]) -> None:
    for item in built.values():
        assert not (item.path / ".git" / "hooks").exists()


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_manifest_validates(name: str) -> None:
    manifest = FIXTURES[name].load_manifest()
    assert manifest.fixture == name


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_manifest_windows_exist_in_built_head(
    name: str, built: dict[str, fixture_build.BuiltFixture]
) -> None:
    repo = built[name].path

    def read(rel: str) -> str | None:
        done = subprocess.run(
            ["git", "show", f"head:{rel}"], cwd=repo, capture_output=True, text=True, check=False
        )
        return done.stdout if done.returncode == 0 else None

    check_against_tree(FIXTURES[name].load_manifest(), read)


@pytest.mark.parametrize("name", sorted(CLEAN))
def test_clean_fixtures_have_no_defects(name: str) -> None:
    assert FIXTURES[name].load_manifest().defects == ()


@pytest.mark.parametrize("name", sorted(REQUIRED - CLEAN))
def test_defective_fixtures_list_defects(name: str) -> None:
    assert FIXTURES[name].load_manifest().defects


def test_mixed_has_a_bug_and_a_nit_in_separate_files() -> None:
    defects = FIXTURES["mixed"].load_manifest().defects
    assert {d.severity for d in defects} == {"major", "info"}
    assert len({d.file for d in defects}) == 2


def test_docs_only_and_test_only_touch_only_their_kind(
    built: dict[str, fixture_build.BuiltFixture],
) -> None:
    docs = git(built["docs-only"].path, "diff", "--name-only", "base", "head").split()
    assert docs and all(p.endswith(".md") for p in docs)
    tests = git(built["test-only"].path, "diff", "--name-only", "base", "head").split()
    assert tests and all(p.startswith("tests/") for p in tests)


def _export(repo: Path, ref: str, dest: Path) -> None:
    """Extract the tree at `ref` into `dest`, which is outside any other project."""
    archive = git_bytes(repo, "archive", "--format=tar", ref)
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True).stdout


@pytest.mark.parametrize("ref", ["base", "head"])
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_fixture_tests_pass_on_both_refs(
    name: str, ref: str, built: dict[str, fixture_build.BuiltFixture], tmp_path: Path
) -> None:
    """Planted bugs slip past the fixture's own tests, so a failure here means a broken fixture."""
    tree = tmp_path / ref
    tree.mkdir()
    _export(built[name].path, ref, tree)
    if not (tree / "tests").is_dir():
        pytest.skip("no tests in this fixture")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if name in FAILS_ON_HEAD and ref == "head":
        assert done.returncode == 1, done.stdout + done.stderr
    else:
        assert done.returncode == 0, done.stdout + done.stderr


def test_unknown_fixture_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(fixture_build.FixtureNotFound):
        fixture_build.build(["nope"], dest=tmp_path)
    assert not list(tmp_path.iterdir())


def test_main_unknown_name_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    assert fixture_build.main(["nope", "--json"]) == ExitCode.NOT_FOUND
    out = capsys.readouterr()
    assert out.out == ""
    assert json.loads(out.err)["error"]["code"] == 3


def test_main_list_prints_names(capsys: pytest.CaptureFixture[str]) -> None:
    assert fixture_build.main(["--list", "--json"]) == ExitCode.OK
    names = [row["name"] for row in json.loads(capsys.readouterr().out)["fixtures"]]
    assert names == sorted(FIXTURES)


def test_discover_rejects_duplicate_names(tmp_path: Path) -> None:
    for root in ("a", "b"):
        for ref in ("base", "head"):
            (tmp_path / root / "same" / ref).mkdir(parents=True)
    with pytest.raises(fixture_build.BuildError, match="used twice"):
        fixture_build.discover([tmp_path / "a", tmp_path / "b"])


def _write_fixture(root: Path, manifest: str) -> None:
    (root / "base").mkdir(parents=True)
    (root / "head").mkdir()
    (root / "base" / "a.py").write_text("x = 1\n")
    (root / "head" / "a.py").write_text("x = 2\ny = 3\n")
    (root / "defects.yaml").write_text(manifest)


GOOD = """\
fixture: sample
summary: s
defects:
  - id: d
    file: a.py
    line_start: 2
    line_end: 2
    severity: minor
    category: logic
    title: t
    description: d
    snippet: "y = 3"
"""


def test_custom_root_builds(tmp_path: Path) -> None:
    _write_fixture(tmp_path / "src" / "sample", GOOD)
    (built_one,) = fixture_build.build(dest=tmp_path / "out", roots=[tmp_path / "src"])
    assert built_one.name == "sample"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("severity: minor", "severity: urgent", "severity"),
        ("line_end: 2", "line_end: 1", "after line_end"),
        ("file: a.py", "file: ../a.py", "relative POSIX"),
        ("category: logic", "category: Logic", "category"),
        ("fixture: sample", "fixture: other", "expected 'sample'"),
        ("    snippet", "    extra: 1\n    snippet", "extra"),
    ],
)
def test_invalid_manifest_is_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    path = tmp_path / "defects.yaml"
    path.write_text(GOOD.replace(old, new))
    with pytest.raises(FixtureManifestError, match=message):
        load_manifest(path, expected_fixture="sample")


def test_duplicate_defect_ids_are_rejected() -> None:
    data = {
        "fixture": "x",
        "summary": "s",
        "defects": [
            {
                "id": "d",
                "file": "a.py",
                "line_start": 1,
                "line_end": 1,
                "severity": "info",
                "category": "style",
                "title": "t",
                "description": "d",
                "snippet": "x",
            }
        ]
        * 2,
    }
    with pytest.raises(ValueError, match="duplicate defect ids"):
        DefectManifest.model_validate(data)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("file: a.py", "file: gone.py", "no file gone.py"),
        ("line_start: 2\n    line_end: 2", "line_start: 3\n    line_end: 9", "past the end"),
        ('snippet: "y = 3"', 'snippet: "z = 4"', "is not in lines"),
    ],
)
def test_stale_manifest_fails_the_build(tmp_path: Path, old: str, new: str, message: str) -> None:
    _write_fixture(tmp_path / "src" / "sample", GOOD.replace(old, new))
    with pytest.raises(FixtureManifestError, match=message):
        fixture_build.build(dest=tmp_path / "out", roots=[tmp_path / "src"])


def test_missing_manifest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FixtureManifestError, match="cannot read"):
        load_manifest(tmp_path / "defects.yaml")
