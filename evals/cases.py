"""The eval case file: its schema, and loading it.

An agent's cases live at `evals/cases.yaml` next to its manifest, under a top-level `cases` key.
See "Eval cases" in docs/CONVENTIONS.md. Every model rejects unknown keys, so a typo in a case
file is an error naming the file and the field instead of a silently ignored expectation.
"""

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from factory.exit import AuthOrConfigError, NotFoundError
from registry.spec import DOMAIN_PATTERN

CASES_FILE = Path("evals") / "cases.yaml"
"""Where an agent's cases live, relative to the directory of its manifest."""

SEVERITIES = ("info", "minor", "major", "blocker")
"""Finding severities, lowest first. A finding meets a `severity_floor` at or above its index."""

type Severity = Literal["info", "minor", "major", "blocker"]
type GraderName = Literal["llm_judge", "sandbox_tests", "jev", "rubric", "incident", "ground_truth"]
type DecisionName = Literal["skip", "review", "escalate"]

RUBRIC_GRADERS = ("rubric", "sandbox_tests")
"""The graders that read `expected.rubric`. On a `sandbox_tests` case the rubric is an extra check
on the prose of an answer whose code the tests already judge, such as a coder's summary."""

GROUND_TRUTH_GRADERS = ("ground_truth", "rubric")
"""The graders that read `expected.ground_truth`. On a `rubric` case the block is an extra check of
an answer whose prose the rubric's judge scores."""

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


class ExpectedCoverage(_Strict):
    """How much line coverage of `paths` the agent's tests must add, measured by `evals.coverage`.

    The grader measures the whole suite twice in the sandbox: on a copy of the work tree with the
    agent's changes undone, and on the work tree as the agent left it. At least `min_gain` lines
    must become covered over all `paths`, and when `max_missing` is set, at most that many may
    still be missing after.
    """

    paths: Annotated[tuple[str, ...], Field(min_length=1)]
    min_gain: Annotated[int, Field(ge=1)] = 1
    max_missing: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _unique_paths(self) -> ExpectedCoverage:
        if len(set(self.paths)) != len(self.paths):
            raise ValueError(f"paths: duplicates in {list(self.paths)}")
        return self


class ExpectedDocDrift(_Strict):
    """What `evals.doc_drift.check_doc_drift` compares, for an agent that writes docs.

    `paths` are the Python files whose public names must have docstrings and be named in the docs;
    `docs` are globs, matched with `PurePosixPath.full_match`, for the docs files that must name
    them, which may include a file the agent is expected to create.
    """

    paths: Annotated[tuple[str, ...], Field(min_length=1)]
    docs: Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _python_paths(self) -> ExpectedDocDrift:
        if len(set(self.paths)) != len(self.paths):
            raise ValueError(f"paths: duplicates in {list(self.paths)}")
        if bad := [path for path in self.paths if not path.endswith(".py")]:
            raise ValueError(f"paths: not Python files: {', '.join(bad)}")
        return self


class ExpectedRun(_Strict):
    """What running the tests in the sandbox must show, for an agent that writes tests or code.

    Each field left out is not checked. `new_tests` runs the files in the answer's `test_files`:
    `fail` means pytest ran and reported failures (exit code 1), `pass` means exit code 0.
    `passes_on_base` also requires those files to pass once the fixture's non-test files are put
    back to their `base` state, which shows the test fails because of the defect and for no other
    reason. `suite` runs every test in the repository. `production` and `tests` say whether the
    agent changed any file that is not, or is, a test file, compared with the fixture's HEAD.
    `coverage` is the line coverage the agent's changes must add (see `ExpectedCoverage`).
    `public_api` lists Python files, relative to the repository, whose public names and signatures
    must be the same in the work tree as at HEAD (see `evals.public_api`), for a refactor.
    `doc_drift` is what the docs and docstrings must say about chosen files (see
    `ExpectedDocDrift` and `evals.doc_drift`), for a docs change.
    """

    new_tests: Literal["fail", "pass"] | None = None
    passes_on_base: bool = False
    suite: Literal["pass"] | None = None
    production: Literal["changed", "unchanged"] | None = None
    tests: Literal["changed", "unchanged"] | None = None
    coverage: ExpectedCoverage | None = None
    public_api: Annotated[tuple[str, ...], Field(min_length=1)] | None = None
    doc_drift: ExpectedDocDrift | None = None

    @model_validator(mode="after")
    def _base_needs_new_tests(self) -> ExpectedRun:
        if self.passes_on_base and self.new_tests is None:
            raise ValueError("passes_on_base: needs new_tests to say which tests to run")
        return self

    @model_validator(mode="after")
    def _public_api_paths(self) -> ExpectedRun:
        if self.public_api is not None:
            if len(set(self.public_api)) != len(self.public_api):
                raise ValueError(f"public_api: duplicates in {list(self.public_api)}")
            if bad := [path for path in self.public_api if not path.endswith(".py")]:
                raise ValueError(f"public_api: not Python files: {', '.join(bad)}")
        return self


class Criterion(_Strict):
    """One rubric criterion, which the rubric grader scores from 0 to 1.

    `text` is what the grader model reads. A `required` criterion fails the case when it scores
    under its own `floor`, whatever the mean; `floor` is needed with `required` and allowed only
    with it.
    """

    id: Annotated[str, Field(pattern=_NAME.pattern)]
    text: Annotated[str, Field(min_length=1)]
    required: bool = False
    floor: Annotated[float, Field(ge=0.0, le=1.0)] | None = None

    @model_validator(mode="after")
    def _floor_with_required(self) -> Criterion:
        if self.required and self.floor is None:
            raise ValueError(f"criterion {self.id}: required needs a floor")
        if not self.required and self.floor is not None:
            raise ValueError(f"criterion {self.id}: floor is only read on a required criterion")
        return self


class Rubric(_Strict):
    """What the `rubric` grader checks, for an agent whose output is prose with no ground truth.

    The deterministic checks run first, without a model: each of `required_fields`, a dotted path
    through the output's mappings, must be present and not empty; each of `empty_fields` must be
    present and empty, for an answer that must hold something back, such as a plan with no goals
    for a request that contradicts itself; no string anywhere in the output may contain one of
    `forbidden` (case-sensitive); and each of `paths_exist`, a dotted path whose
    value is a path or a list of paths, must name files or directories inside the fixture
    repository. Only an answer that passes them is scored, and it passes when the mean of the
    `criteria` scores reaches `threshold` and every required criterion meets its floor.
    """

    criteria: Annotated[tuple[Criterion, ...], Field(min_length=1)]
    threshold: Annotated[float, Field(ge=0.0, le=1.0)]
    required_fields: tuple[str, ...] = ()
    empty_fields: tuple[str, ...] = ()
    forbidden: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    paths_exist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _unique_criteria(self) -> Rubric:
        ids = [criterion.id for criterion in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError(f"criteria: duplicate ids in {ids}")
        if both := sorted(set(self.required_fields) & set(self.empty_fields)):
            raise ValueError(f"required_fields and empty_fields both name {', '.join(both)}")
        return self


class CitationsCheck(_Strict):
    """What the `citations_resolve` check reads, for a research agent whose claims carry citations.

    `claims` is a dotted path through the output to a list of claims (see
    `registry.common_schemas.Claim`). Each citation of each claim marked as a fact is fetched
    through `web.fetch`, and its quote must be in the page. `allow_domains` is the case's web
    allow-list, as `tools.web.allow_domains` is for an agent run: a cited host that is not one of
    them or a subdomain of one is a case error. It cannot be empty, and each entry is a bare
    lowercase host, as in a manifest. The eval runner also runs the agent with this list in place
    of its manifest's `tools.web.allow_domains` (see `registry.spec.LLMSpec.with_web_domains`).
    """

    claims: Annotated[str, Field(min_length=1)] = "claims"
    allow_domains: Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]

    @field_validator("allow_domains")
    @classmethod
    def _bare_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for domain in value:
            if not re.fullmatch(DOMAIN_PATTERN, domain):
                raise ValueError(f"allow_domains entries are bare lowercase hosts, got {domain!r}")
        return value


class ExpectedIncident(_Strict):
    """What the `incident` grader checks in a triage-diagnostics answer.

    `suspected_path` is the file of the planted fault, which the answer's `suspected_paths` must
    list. `exception` is the exception class the event shows, such as `ZeroDivisionError`, and
    `crash_file` the repository path of the frame that raised it: the answer's reproduction test
    must fail with that exception, raised from that file, when run at the fixture's `head`, which
    plays the release tag. The two files differ when the fault is above the raising frame.
    """

    suspected_path: Annotated[str, Field(min_length=1)]
    exception: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    crash_file: Annotated[str, Field(min_length=1)]


class ExpectedGroundTruth(_Strict):
    """What an answer must hold of the `ground-truth.yaml` of the case's fixture, by entry id.

    Each field names entries of the section of the same name, and the rules that match an answer
    against them are in `evals/graders/ground_truth.py`. A case repeats none of the text of an
    entry, so the ground truth stays the one place that says what is true of the fixture. The
    check that no decoy is called dead needs no id: it covers every decoy of the fixture.
    """

    dead_code: tuple[str, ...] = ()
    data_flows: tuple[str, ...] = ()
    call_chains: tuple[str, ...] = ()
    dynamic_calls: tuple[str, ...] = ()
    features: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _names_entries(self) -> ExpectedGroundTruth:
        named = {field: getattr(self, field) for field in type(self).model_fields}
        if not any(named.values()):
            raise ValueError("name at least one entry of the ground truth")
        for field, ids in named.items():
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"{field}: duplicate ids: {', '.join(dupes)}")
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

    `rubric` is what the `rubric` grader scores, and is required for that grader. A
    `sandbox_tests` case may set it too: its checks and criteria then run after the checks of
    `run`, and the rubric grader takes the place of the LLM judge.
    `citations_resolve` turns on the check of that name, which only a `rubric` case may use.
    `options_in_record` turns on the check that every option of an escalation summary appears in
    the record the case's payload holds (`runtime.escalation_summary.ungrounded`, the same code
    that runs before a summary is stored); only a `rubric` case may use it.
    `incident` is what the `incident` grader checks, and is required for that grader.
    `ground_truth` names the entries of the fixture's ground truth an answer must hold. It is
    required for the `ground_truth` grader and may be set on a `rubric` case. It is not part of the
    dumped model, so no judge sees the ids.
    """

    verdict: str | None
    defects: tuple[ExpectedDefect, ...] = ()
    criteria: str | None = None
    run: ExpectedRun | None = None
    answers: dict[str, str] | None = None
    decision: DecisionName | None = None
    risky: bool = Field(default=False, exclude=True)
    rubric: Rubric | None = None
    citations_resolve: CitationsCheck | None = None
    options_in_record: bool = Field(default=False, exclude=True)
    incident: ExpectedIncident | None = None
    ground_truth: ExpectedGroundTruth | None = Field(default=None, exclude=True)

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
        if self.grader == "rubric" and self.expected.rubric is None:
            raise ValueError("expected.rubric: required by the rubric grader")
        if self.grader not in RUBRIC_GRADERS and self.expected.rubric is not None:
            raise ValueError("expected.rubric: only the rubric and sandbox_tests graders read it")
        if self.grader != "rubric" and self.expected.citations_resolve is not None:
            raise ValueError("expected.citations_resolve: only a rubric case may use it")
        if self.grader != "rubric" and self.expected.options_in_record:
            raise ValueError("expected.options_in_record: only a rubric case may use it")
        if self.grader == "incident" and self.expected.incident is None:
            raise ValueError("expected.incident: required by the incident grader")
        if self.grader != "incident" and self.expected.incident is not None:
            raise ValueError("expected.incident: only the incident grader reads it")
        if self.grader == "ground_truth" and self.expected.ground_truth is None:
            raise ValueError("expected.ground_truth: required by the ground_truth grader")
        if self.grader not in GROUND_TRUTH_GRADERS and self.expected.ground_truth is not None:
            raise ValueError(
                "expected.ground_truth: only the ground_truth and rubric graders read it"
            )
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
