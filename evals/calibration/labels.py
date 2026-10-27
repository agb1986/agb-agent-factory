"""The triage calibration labels: their schema, and loading them.

`evals/calibration/labels.yaml` holds historical diffs, each labelled `risky` or `safe` with a
one-line reason. A diff is stored as a GitHub repository URL with a base and a head commit SHA,
never as source, and is fetched again at run time. The comment at the top of `labels.yaml` says
how the rows were chosen and labelled. Every model rejects unknown keys, so a typo in the file is
an error naming the row and the field instead of a silently ignored label.
"""

import datetime
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from factory.exit import AuthOrConfigError, NotFoundError

LABELS_FILE = Path(__file__).parent / "labels.yaml"
"""The labelled diffs that calibration runs over."""

MAX_REASON_CHARS = 160
"""A reason is one line; anything longer is probably quoting the diff."""

type Label = Literal["risky", "safe"]
type Severity = Literal["blocker", "major", "minor", "none"]
"""The worst finding the labeller expects, on the reviewers' scale (agents/code-reviewer/system.md).
`none` means nothing worth a comment. A diff is risky exactly when this is `major` or `blocker`."""

RISKY_SEVERITIES: frozenset[str] = frozenset({"blocker", "major"})
type Source = Literal["panel", "reader"]
"""`panel`: the full review panel reported (or did not report) a major or blocker finding.
`reader`: someone reading the diff judged whether a careful reviewer would."""

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


class LabelFileError(AuthOrConfigError):
    """The labels file is malformed or fails validation (exit 4). The message names the field."""


class LabelsNotFound(NotFoundError):
    """There is no labels file (exit 3)."""


def _sha(value: str) -> str:
    if not _SHA.match(value):
        raise ValueError(f"{value!r} is not a full 40-character lowercase commit SHA")
    return value


def _reason(value: str) -> str:
    if not value.strip() or "\n" in value:
        raise ValueError("reason must be one non-empty line")
    if len(value) > MAX_REASON_CHARS:
        raise ValueError(f"reason is {len(value)} characters, more than {MAX_REASON_CHARS}")
    return value


type Sha = Annotated[str, AfterValidator(_sha)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabelledDiff(_Strict):
    """One historical diff: where to fetch it, its label, and who labelled it why and when.

    `source` says which test made it risky or safe: the full panel's findings, or a reader's
    judgment of what a careful reviewer would find.

    `needs_panel` marks a reader label its labeller was unsure of; it is to be checked against a
    full panel run, and is still a label until then.
    """

    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+#\d+$")]
    repo: Annotated[str, Field(pattern=_REPO.pattern)]
    base: Sha
    head: Sha
    private: bool
    label: Label
    severity: Severity
    reason: Annotated[str, AfterValidator(_reason)]
    source: Source
    labelled_by: Annotated[str, Field(min_length=1)]
    labelled_on: datetime.date
    needs_panel: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> LabelledDiff:
        if self.base == self.head:
            raise ValueError("base and head are the same commit")
        if (self.severity in RISKY_SEVERITIES) != (self.label == "risky"):
            raise ValueError(
                f"label {self.label!r} contradicts severity {self.severity!r}: "
                "a diff is risky exactly when its worst finding is major or blocker"
            )
        return self

    @property
    def owner_repo(self) -> str:
        """The repository as `owner/name`, as the GitHub API addresses it."""
        match = _REPO.match(self.repo)
        assert match is not None  # the field pattern guarantees it
        return f"{match[1]}/{match[2]}"

    @property
    def risky(self) -> bool:
        """Whether the diff is labelled risky."""
        return self.label == "risky"


class LabelFile(_Strict):
    """The whole file: at least one diff, each with its own id and its own commit pair."""

    diffs: Annotated[tuple[LabelledDiff, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> LabelFile:
        ids: set[str] = set()
        pairs: set[tuple[str, str, str]] = set()
        for diff in self.diffs:
            if diff.id in ids:
                raise ValueError(f"diffs: duplicate id {diff.id!r}")
            pair = (diff.repo, diff.base, diff.head)
            if pair in pairs:
                raise ValueError(f"diffs: {diff.id!r} repeats the commit pair of an earlier row")
            ids.add(diff.id)
            pairs.add(pair)
        return self


def load_labels(path: Path = LABELS_FILE) -> list[LabelledDiff]:
    """Read and validate a labels file and return its diffs in file order.

    Raises `LabelsNotFound` when the file does not exist and `LabelFileError` for unreadable or
    malformed YAML or any schema violation; the message is one line naming the file and each
    failing field.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LabelsNotFound(f"no calibration labels at {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise LabelFileError(f"{path}: cannot read labels: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LabelFileError(f"{path}: malformed YAML: {' '.join(str(exc).split())}") from exc
    if not isinstance(data, dict):
        raise LabelFileError(f"{path}: labels file must be a mapping with a `diffs` key")
    try:
        return list(LabelFile.model_validate(data).diffs)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '(file)'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        )
        raise LabelFileError(f"{path}: {problems}") from exc
