"""Tests for evals/calibration/labels.py: the label schema and `load_labels`."""

import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.calibration.labels import (
    LabelFileError,
    LabelledDiff,
    LabelsNotFound,
    load_labels,
)
from factory.exit import ExitCode, exit_code

BASE = "a" * 40
HEAD = "b" * 40


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "shop#7",
        "repo": "https://github.com/acme/shop",
        "base": BASE,
        "head": HEAD,
        "private": True,
        "label": "risky",
        "severity": "major",
        "reason": "Changes the free-shipping boundary from >= to >.",
        "source": "reader",
        "labelled_by": "claude-opus-5-5",
        "labelled_on": "2026-10-27",
    }
    return row | overrides


def _load(tmp_path: Path, *rows: dict[str, Any]) -> list[LabelledDiff]:
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump({"diffs": list(rows)}))
    return load_labels(path)


def _rejects(tmp_path: Path, *rows: dict[str, Any]) -> str:
    with pytest.raises(LabelFileError) as info:
        _load(tmp_path, *rows)
    assert exit_code(info.value) == ExitCode.AUTH_OR_CONFIG
    return info.value.message


def test_a_valid_row_loads_with_its_derived_fields(tmp_path: Path) -> None:
    (diff,) = _load(tmp_path, _row())
    assert diff.owner_repo == "acme/shop"
    assert diff.risky is True
    assert diff.labelled_on == datetime.date(2026, 10, 27)
    assert diff.needs_panel is False


def test_a_safe_row_with_no_finding_is_not_risky(tmp_path: Path) -> None:
    (diff,) = _load(tmp_path, _row(label="safe", severity="none", source="panel"))
    assert diff.risky is False


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        (_row(base="abc123"), "base"),
        (_row(head="B" * 40), "head"),
        (_row(head=BASE), "same commit"),
        (_row(repo="git@github.com:acme/shop.git"), "repo"),
        (_row(id="no number"), "id"),
        (_row(label="maybe"), "label"),
        (_row(severity="huge"), "severity"),
        (_row(source="vibes"), "source"),
        (_row(reason=""), "reason"),
        (_row(reason="two\nlines"), "reason"),
        (_row(reason="x" * 161), "reason"),
        (_row(labelled_by=""), "labelled_by"),
        (_row(labelled_on="last week"), "labelled_on"),
        (_row(diff="--- a/x.py"), "diff"),
        (_row(label="safe", severity="major"), "contradicts"),
        (_row(label="risky", severity="minor"), "contradicts"),
    ],
)
def test_an_invalid_row_is_rejected_naming_the_file_and_field(
    tmp_path: Path, row: dict[str, Any], fragment: str
) -> None:
    message = _rejects(tmp_path, row)
    assert "labels.yaml" in message
    assert fragment in message


def test_a_row_without_a_reason_is_rejected(tmp_path: Path) -> None:
    row = _row()
    del row["reason"]
    assert "reason" in _rejects(tmp_path, row)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    assert "duplicate" in _rejects(tmp_path, _row(), _row(base="c" * 40))


def test_a_repeated_commit_pair_is_rejected(tmp_path: Path) -> None:
    assert "commit pair" in _rejects(tmp_path, _row(), _row(id="shop#8"))


def test_an_empty_list_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "labels.yaml"
    path.write_text("diffs: []\n")
    with pytest.raises(LabelFileError):
        load_labels(path)


def test_a_file_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "labels.yaml"
    path.write_text("- one\n- two\n")
    with pytest.raises(LabelFileError, match="mapping"):
        load_labels(path)


def test_a_file_that_is_not_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "labels.yaml"
    path.write_text("diffs: [unclosed\n")
    with pytest.raises(LabelFileError, match="malformed YAML"):
        load_labels(path)


def test_a_missing_file_is_not_found_with_exit_code_3(tmp_path: Path) -> None:
    with pytest.raises(LabelsNotFound) as info:
        load_labels(tmp_path / "nope.yaml")
    assert exit_code(info.value) == ExitCode.NOT_FOUND
