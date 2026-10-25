"""The eval case file: its schema, and loading it.

An agent's cases live at `evals/cases.yaml` next to its manifest, under a top-level `cases` key.
See "Eval cases" in docs/CONVENTIONS.md. Every model rejects unknown keys, so a typo in a case
file is an error naming the file and the field instead of a silently ignored expectation.
"""

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from factory.exit import AuthOrConfigError, NotFoundError

CASES_FILE = Path("evals") / "cases.yaml"
"""Where an agent's cases live, relative to the directory of its manifest."""

SEVERITIES = ("info", "minor", "major", "blocker")
"""Finding severities, lowest first. A finding meets a `severity_floor` at or above its index."""

type Severity = Literal["info", "minor", "major", "blocker"]
type GraderName = Literal["llm_judge", "jev"]

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class CaseFileError(AuthOrConfigError):
    """A cases file is malformed or fails validation (exit 4). The message names file and field."""


class CasesNotFound(NotFoundError):
    """An agent has no cases file (exit 3)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedDefect(_Strict):
    """One defect the agent is expected to report, in the head tree of the case's fixture."""

    id: str
    file: str
    line_start: Annotated[int, Field(ge=1)]
    line_end: Annotated[int, Field(ge=1)]
    severity_floor: Severity

    @model_validator(mode="after")
    def _ordered_lines(self) -> ExpectedDefect:
        if self.line_end < self.line_start:
            raise ValueError(f"line_end {self.line_end} is before line_start {self.line_start}")
        return self


class Expected(_Strict):
    """What a good answer looks like: the verdict and the defects that must be found."""

    verdict: str
    defects: tuple[ExpectedDefect, ...] = ()


class Case(_Strict):
    """One eval case.

    `fixture` names a repository built from `fixtures/` and `payload` is merged into the agent's
    input; `repo_path` is not part of it, because the runner fills it in with the built fixture.
    """

    id: Annotated[str, Field(pattern=_NAME.pattern)]
    fixture: Annotated[str, Field(pattern=_NAME.pattern)]
    payload: dict[str, Any] = Field(default_factory=dict)
    expected: Expected
    grader: GraderName

    @model_validator(mode="after")
    def _no_repo_path(self) -> Case:
        if "repo_path" in self.payload:
            raise ValueError("payload.repo_path: not allowed, the runner sets it")
        return self


class CaseFile(_Strict):
    """The whole file: at least one case, each with its own id."""

    cases: Annotated[tuple[Case, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_ids(self) -> CaseFile:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"cases: duplicate id {case.id!r}")
            seen.add(case.id)
        return self


def load_cases(path: Path) -> list[Case]:
    """Read and validate a cases file and return its cases in file order.

    Raises `CasesNotFound` when the file does not exist and `CaseFileError` for unreadable or
    malformed YAML or any schema violation; the message is one line naming the file and each
    failing field.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CasesNotFound(
            f"no eval cases at {path}", hint=f"add a `cases:` list in {path}"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise CaseFileError(f"{path}: cannot read cases: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CaseFileError(f"{path}: malformed YAML: {' '.join(str(exc).split())}") from exc
    if not isinstance(data, dict):
        raise CaseFileError(f"{path}: cases file must be a mapping with a `cases` key")
    try:
        return list(CaseFile.model_validate(data).cases)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '(file)'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        )
        raise CaseFileError(f"{path}: {problems}") from exc
