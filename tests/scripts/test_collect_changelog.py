"""Tests for scripts/collect_changelog.py, run against throwaway changelogs and fragments."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "collect_changelog.py"

CHANGELOG = """\
# Changelog

Intro line.

## [Unreleased]

### Fixed

- An old fix (#1).

### Added

- An old addition (#2).

## [0.1.0] - 2026-01-01

### Added

- The first release.

<!-- a comment above the links -->
[Unreleased]: https://example.invalid/compare/v0.1.0...main
[0.1.0]: https://example.invalid/releases/tag/v0.1.0
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collect_changelog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script() -> ModuleType:
    return _load()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "README.md").write_text("# not a fragment\n", encoding="utf-8")
    return tmp_path


def _run(script: ModuleType, repo: Path, *args: str) -> int:
    return script.main(
        [
            "--changelog",
            str(repo / "CHANGELOG.md"),
            "--fragments",
            str(repo / "changelog.d"),
            *args,
        ]
    )


def _fragment(repo: Path, name: str, text: str) -> Path:
    path = repo / "changelog.d" / name
    path.write_text(text, encoding="utf-8")
    return path


def test_nothing_to_collect_leaves_the_changelog_alone(
    script: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(script, repo) == 0
    assert "no fragments" in capsys.readouterr().out
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG


def test_entries_are_appended_to_their_sections_in_id_order(script: ModuleType, repo: Path) -> None:
    _fragment(repo, "20.added.md", "- Added by twenty (#20).\n")
    _fragment(repo, "3.added.md", "- Added by three (#3).\n")
    _fragment(repo, "slug.added.md", "- Added by a slug.\n")
    _fragment(repo, "7.fixed.md", "- Fixed by seven (#7).\n")
    assert _run(script, repo) == 0
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text[text.index("## [Unreleased]") : text.index("## [0.1.0]")]
    assert unreleased == (
        "## [Unreleased]\n\n"
        "### Fixed\n\n- An old fix (#1).\n- Fixed by seven (#7).\n\n"
        "### Added\n\n- An old addition (#2).\n- Added by three (#3).\n- Added by twenty (#20).\n"
        "- Added by a slug.\n\n"
    )
    assert text.endswith("[0.1.0]: https://example.invalid/releases/tag/v0.1.0\n")
    assert "- The first release.\n" in text  # the released section is untouched


def test_fragments_are_deleted_but_the_readme_stays(script: ModuleType, repo: Path) -> None:
    _fragment(repo, "3.added.md", "- Added by three (#3).\n")
    assert _run(script, repo) == 0
    assert [p.name for p in (repo / "changelog.d").iterdir()] == ["README.md"]


def test_a_missing_section_is_created_in_keep_a_changelog_order(
    script: ModuleType, repo: Path
) -> None:
    _fragment(repo, "4.changed.md", "- Changed by four (#4).\n")
    _fragment(repo, "5.security.md", "- Secured by five (#5).\n")
    assert _run(script, repo) == 0
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text[text.index("## [Unreleased]") : text.index("## [0.1.0]")]
    assert unreleased == (
        "## [Unreleased]\n\n"
        "### Changed\n\n- Changed by four (#4).\n\n"
        "### Fixed\n\n- An old fix (#1).\n\n"
        "### Added\n\n- An old addition (#2).\n\n"
        "### Security\n\n- Secured by five (#5).\n\n"
    )


def test_an_empty_unreleased_section_gets_its_first_heading(script: ModuleType, repo: Path) -> None:
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n\n- Old.\n", encoding="utf-8"
    )
    _fragment(repo, "9.added.md", "- New (#9).\n")
    assert _run(script, repo) == 0
    assert changelog.read_text(encoding="utf-8") == (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- New (#9).\n\n"
        "## [0.1.0] - 2026-01-01\n\n- Old.\n"
    )


def test_continuation_lines_and_several_entries_are_kept(script: ModuleType, repo: Path) -> None:
    _fragment(repo, "8.added.md", "\n- First (#8).\n  More about it.\n- Second (#8).\n\n")
    assert _run(script, repo) == 0
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- An old addition (#2).\n- First (#8).\n  More about it.\n- Second (#8).\n\n## " in text


def test_dry_run_prints_and_changes_nothing(
    script: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _fragment(repo, "3.added.md", "- Added by three (#3).\n")
    assert _run(script, repo, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "### Added\n- Added by three (#3).\n" in out and "dry run" in out
    assert path.exists()
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("3.bogus.md", "- x\n", "section must be one of"),
        ("notes.md", "- x\n", "name it <id>.<section>.md"),
        ("3.added.md", "\n\n", "the fragment is empty"),
        ("3.added.md", "Added by three (#3).\n", "an entry starts with '- '"),
        ("3.added.md", "- Added (#3).\nnot indented\n", "a continuation line is indented"),
    ],
)
def test_bad_fragments_exit_2_and_change_nothing(
    script: ModuleType,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    text: str,
    message: str,
) -> None:
    path = _fragment(repo, name, text)
    assert _run(script, repo) == 2
    assert message in capsys.readouterr().err
    assert path.exists()
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG


def test_changelog_without_unreleased_exits_2(
    script: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [0.1.0]\n", encoding="utf-8")
    _fragment(repo, "3.added.md", "- x (#3).\n")
    assert _run(script, repo) == 2
    assert "no '## [Unreleased]' heading" in capsys.readouterr().err


def test_missing_changelog_exits_2(
    script: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "CHANGELOG.md").unlink()
    _fragment(repo, "3.added.md", "- x (#3).\n")
    assert _run(script, repo) == 2
    assert "no such file" in capsys.readouterr().err


def test_the_repository_fragments_are_well_formed(script: ModuleType) -> None:
    root = Path(__file__).parents[2]
    fragments = script.read_fragments(root / "changelog.d")
    assert all(f.section in script.SECTIONS for f in fragments)
    text, _ = script.merge((root / "CHANGELOG.md").read_text(encoding="utf-8"), fragments)
    assert "## [Unreleased]" in text
