"""Input and output models of the triage-diagnostics agent.

The agent reads a Sentry event and the repository at the event's release tag and writes the
diagnosis an incident ticket is made from: a title, reproduction steps, the suspected paths, the
place the fault is, a confidence and a draft test that reproduces the crash. The event models
mirror the document `sentry.read_issue` returns, so a payload saved from that tool is valid input
as it is. The agent writes nothing to the work-item store: `pipelines.incident` files the ticket
(ADR 0002). It renders the stack trace from the event itself, so the agent does not copy it.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PATH_RULE = "must be a relative POSIX path inside the repository"


def _check_path(path: str, what: str) -> str:
    if not path or path.startswith("/") or "\\" in path or ".." in path.split("/"):
        raise ValueError(f"{what} {path!r} {_PATH_RULE}")
    return path


class SentryFrame(BaseModel):
    """One stack frame, as `sentry.read_issue` returns it. Innermost frames come last."""

    model_config = ConfigDict(extra="forbid")

    file: str | None = Field(default=None, description="The frame's file name or absolute path.")
    function: str | None = Field(default=None, description="The function the frame is in.")
    line: int | None = Field(default=None, description="The 1-based line number in the file.")
    in_app: bool | None = Field(
        default=None, description="Whether Sentry counts the frame as application code."
    )


class SentryException(BaseModel):
    """One exception of the event with its innermost frames."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, description="The exception class, such as `KeyError`.")
    value: str | None = Field(default=None, description="The exception message.")
    frames: list[SentryFrame] = Field(
        default_factory=list, description="The stack frames, outermost first."
    )


class SentryBreadcrumb(BaseModel):
    """One breadcrumb recorded before the failure."""

    model_config = ConfigDict(extra="forbid")

    time: str | None = Field(default=None, description="When it was recorded, ISO 8601.")
    category: str | None = Field(default=None, description="The breadcrumb's category.")
    level: str | None = Field(default=None, description="Its level, such as `info`.")
    message: str | None = Field(default=None, description="What it says.")


class SentryEvent(BaseModel):
    """A Sentry issue and its latest event, in the shape `sentry.read_issue` returns."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="The numeric Sentry issue id.")
    short_id: str | None = Field(default=None, description="The issue's short id, like `APP-12`.")
    title: str = Field(min_length=1, description="The issue title Sentry shows.")
    culprit: str | None = Field(default=None, description="Where Sentry thinks the fault is.")
    level: str | None = Field(default=None, description="The level, such as `error`.")
    status: str | None = Field(default=None, description="The issue status, such as `unresolved`.")
    count: int = Field(default=0, ge=0, description="How many events the issue has.")
    user_count: int | None = Field(default=None, ge=0, description="How many users it hit.")
    first_seen: str | None = Field(default=None, description="When it was first seen, ISO 8601.")
    last_seen: str | None = Field(default=None, description="When it was last seen, ISO 8601.")
    release: str | None = Field(default=None, description="The release tag of the latest event.")
    first_release: str | None = Field(default=None, description="The first release it was seen in.")
    last_release: str | None = Field(default=None, description="The last release it was seen in.")
    event_id: str | None = Field(default=None, description="The id of the latest event.")
    exceptions: list[SentryException] = Field(
        min_length=1, description="The exceptions of the latest event; the last one was raised."
    )
    breadcrumbs: list[SentryBreadcrumb] = Field(
        default_factory=list, description="The breadcrumbs before the failure, oldest first."
    )


class TriageDiagnosticsInput(BaseModel):
    """The event to diagnose and the repository, checked out at the event's release."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    event: SentryEvent = Field(description="The Sentry issue and its latest event.")
    ref: str = Field(
        min_length=1,
        description="The git ref of the release the event came from: pass it to `git.read_file`.",
    )


class Culprit(BaseModel):
    """The lines where the fault is, which may be above the frame that raised."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, description="Path of the file, relative to the root.")
    line_start: int = Field(ge=1, description="First line of the fault, 1-based.")
    line_end: int = Field(ge=1, description="Last line of the fault, inclusive.")
    reason: str = Field(min_length=1, description="Why these lines cause the event.")

    @field_validator("file")
    @classmethod
    def _relative(cls, value: str) -> str:
        return _check_path(value, "culprit file")

    @model_validator(mode="after")
    def _ordered(self) -> Culprit:
        if self.line_end < self.line_start:
            raise ValueError("line_end must not be before line_start")
        return self


class ReproductionTest(BaseModel):
    """A pytest file that fails at the release for the reason the event shows."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1, description="Where the file goes, relative to the root, such as `tests/x.py`."
    )
    code: str = Field(min_length=1, description="The whole file: imports and test functions.")

    @field_validator("path")
    @classmethod
    def _test_file(cls, value: str) -> str:
        _check_path(value, "reproduction test path")
        name = value.rsplit("/", 1)[-1]
        if not (name.startswith("test_") and name.endswith(".py")):
            raise ValueError(f"reproduction test path {value!r} must name a test_*.py file")
        return value


class TriageDiagnosticsOutput(BaseModel):
    """The diagnosis of one event: what an incident ticket needs beyond the event itself."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=1,
        max_length=120,
        description="The ticket title: one line naming the failure and where it is.",
    )
    summary: str = Field(
        min_length=1, description="Two to four sentences: what fails, when and why."
    )
    reproduction_steps: list[str] = Field(
        min_length=1, description="The steps that make the failure happen, in order."
    )
    suspected_paths: list[str] = Field(
        min_length=1,
        description="Files the fix may touch: the culprit's file first, then what `deps.impact` "
        "returns for it.",
    )
    culprit: Culprit = Field(description="The lines where the fault is.")
    confidence: float = Field(ge=0.0, le=1.0, description="How sure the diagnosis is, from 0 to 1.")
    reproduction_test: ReproductionTest = Field(
        description="A test file that fails at the release for the reason the event shows."
    )

    @field_validator("title")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if "\n" in value or not value.strip():
            raise ValueError("title must be one non-blank line")
        return value

    @field_validator("reproduction_steps")
    @classmethod
    def _no_blank_steps(cls, value: list[str]) -> list[str]:
        if any(not step.strip() for step in value):
            raise ValueError("reproduction_steps must not hold a blank step")
        return value

    @field_validator("suspected_paths")
    @classmethod
    def _repository_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            _check_path(path, "suspected path")
        if len(set(value)) != len(value):
            raise ValueError("suspected_paths must not repeat a path")
        return value
