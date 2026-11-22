"""Schema for `defects.yaml`, the list of defects planted in one fixture, and its checker.

A manifest names every defect by file, line window, severity and category. Paths and line numbers
refer to the fixture's `head` tree. An eval case scores an agent's findings against this list, so
the windows must stay exact: `check_against_tree` fails when a window drifts off its code.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from factory.exit import AuthOrConfigError

SLUG_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"

type Slug = Annotated[str, Field(pattern=SLUG_PATTERN)]
type Severity = Literal["info", "minor", "major", "blocker"]


class FixtureManifestError(AuthOrConfigError):
    """A `defects.yaml` or `ground-truth.yaml` is missing, invalid or out of step with its tree."""


class _Model(BaseModel):
    """Base for every manifest block: unknown keys are errors and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Defect(_Model):
    """One planted defect.

    `file` is a relative POSIX path inside the head tree. `line_start` and `line_end` are 1-based
    and inclusive. `snippet` is text that must appear inside that window, so a manifest cannot
    silently point at the wrong code after a source edit. `severity` uses the values of a finding.
    `category` is a lowercase slug; the fixtures use logic, style, security, performance, testing
    and dead-code.
    """

    id: Slug
    file: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    severity: Severity
    category: Slug
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    snippet: str = Field(min_length=1)

    @field_validator("file")
    @classmethod
    def _relative_posix(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError(f"file must be a relative POSIX path inside the tree: {value!r}")
        return value

    @model_validator(mode="after")
    def _ordered_window(self) -> Defect:
        if self.line_start > self.line_end:
            raise ValueError(f"line_start {self.line_start} is after line_end {self.line_end}")
        return self


class DefectManifest(_Model):
    """The whole `defects.yaml`: the fixture name, a one-paragraph summary and its defects.

    An empty `defects` list marks a fixture that a reviewer should leave without findings.
    """

    fixture: Slug
    summary: str = Field(min_length=1)
    defects: tuple[Defect, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> DefectManifest:
        ids = [defect.id for defect in self.defects]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate defect ids: {', '.join(dupes)}")
        return self


def load_manifest(path: Path, *, expected_fixture: str | None = None) -> DefectManifest:
    """Read and validate the `defects.yaml` at `path`.

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
        manifest = DefectManifest.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise FixtureManifestError(f"{path} is invalid: {details}") from exc
    if expected_fixture is not None and manifest.fixture != expected_fixture:
        raise FixtureManifestError(
            f"{path} names fixture {manifest.fixture!r}, expected {expected_fixture!r}",
            hint="the fixture field must equal the fixture's directory name",
        )
    return manifest


def check_against_tree(manifest: DefectManifest, read_file: Callable[[str], str | None]) -> None:
    """Check every defect against the head tree, which `read_file` serves.

    `read_file(path)` returns the text of the file at the relative POSIX `path`, or None when the
    file does not exist. Raises `FixtureManifestError` listing every defect whose file is missing,
    whose window ends past the last line, or whose window does not contain its snippet.
    """
    problems: list[str] = []
    for defect in manifest.defects:
        text = read_file(defect.file)
        if text is None:
            problems.append(f"{defect.id}: no file {defect.file} in the head tree")
            continue
        lines = text.splitlines()
        if defect.line_end > len(lines):
            problems.append(
                f"{defect.id}: window {defect.line_start}-{defect.line_end} is past the end of "
                f"{defect.file} ({len(lines)} lines)"
            )
            continue
        window = "\n".join(lines[defect.line_start - 1 : defect.line_end])
        if defect.snippet not in window:
            problems.append(
                f"{defect.id}: snippet {defect.snippet!r} is not in lines "
                f"{defect.line_start}-{defect.line_end} of {defect.file}"
            )
    if problems:
        raise FixtureManifestError(
            f"fixture {manifest.fixture!r} does not match its head tree: " + "; ".join(problems),
            hint="update line_start and line_end in defects.yaml after editing the source tree",
        )
