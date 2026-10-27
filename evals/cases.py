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
type GraderName = Literal["llm_judge", "sandbox_tests", "jev"]
type DecisionName = Literal["skip", "review", "escalate"]

FIXTURE_REF = "$fixture"
"""The key of a payload value the runner replaces with content of the case's fixture."""

FIXTURE_CONTENT = ("diff", "files")
"""What a `$fixture` reference may name: the base-to-head diff, or the files it changes."""

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


class ExpectedRun(_Strict):
    """What running the tests in the sandbox must show, for an agent that writes tests or code.

    Each field left out is not checked. `new_tests` runs the files in the answer's `test_files`:
    `fail` means pytest ran and reported failures (exit code 1), `pass` means exit code 0.
    `passes_on_base` also requires those files to pass once the fixture's non-test files are put
    back to their `base` state, which shows the test fails because of the defect and for no other
    reason. `suite` runs every test in the repository. `production` and `tests` say whether the
    agent changed any file that is not, or is, a test file, compared with the fixture's HEAD.
    """

    new_tests: Literal["fail", "pass"] | None = None
    passes_on_base: bool = False
    suite: Literal["pass"] | None = None
    production: Literal["changed", "unchanged"] | None = None
    tests: Literal["changed", "unchanged"] | None = None

    @model_validator(mode="after")
    def _base_needs_new_tests(self) -> ExpectedRun:
        if self.passes_on_base and self.new_tests is None:
            raise ValueError("passes_on_base: needs new_tests to say which tests to run")
        return self


class Expected(_Strict):
    """What a good answer looks like: the verdict and the defects that must be found.

    `verdict` is required but may be null for an agent whose output has none, such as the judge;
    a null verdict is not checked. `criteria` is prose the grader must also see met, such as "the
    rationale names the failed reviewer"; the deterministic checks do not read it. `run` is what
    the `sandbox_tests` grader checks by running tests, and is required for that grader.

    For a Jev agent, `answers` maps a choice or score question id to the option it must pick, and
    `decision` is what `runtime.gates.decide` must make of the output: `skip`, `escalate`, or
    `review` for neither. `risky` marks a case whose diff carries a major or blocker defect, which
    the runner's `recall_risky` counts; it needs `decision`, and the graders never see it.
    """

    verdict: str | None
    defects: tuple[ExpectedDefect, ...] = ()
    criteria: str | None = None
    run: ExpectedRun | None = None
    answers: dict[str, str] | None = None
    decision: DecisionName | None = None
    risky: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _risky_needs_decision(self) -> Expected:
        if self.risky and self.decision is None:
            raise ValueError("risky: only meaningful with decision")
        return self


class Case(_Strict):
    """One eval case.

    `fixture` names a repository built from `fixtures/` and `payload` is merged into the agent's
    input; `repo_path` is not part of it, because the runner fills it in with the built fixture.
    A payload value `{"$fixture": "diff"}` or `{"$fixture": "files"}` is replaced by the runner with
    the fixture's base-to-head diff or the list of files it changes.
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

    @model_validator(mode="after")
    def _fixture_refs(self) -> Case:
        for key, value in self.payload.items():
            if not isinstance(value, dict) or FIXTURE_REF not in value:
                continue
            if len(value) != 1 or value[FIXTURE_REF] not in FIXTURE_CONTENT:
                raise ValueError(
                    f"payload.{key}: a {FIXTURE_REF} reference is one key naming "
                    f"{' or '.join(FIXTURE_CONTENT)}, not {value!r}"
                )
        return self

    @model_validator(mode="after")
    def _run_matches_grader(self) -> Case:
        if self.grader == "sandbox_tests" and self.expected.run is None:
            raise ValueError("expected.run: required by the sandbox_tests grader")
        if self.grader != "sandbox_tests" and self.expected.run is not None:
            raise ValueError("expected.run: only the sandbox_tests grader reads it")
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
