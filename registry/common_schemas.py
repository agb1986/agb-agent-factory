"""Review models shared by every reviewer agent.

The output model is sent to the Messages API as a JSON schema (`output_config.format`), so every
field is a plain type, a `Literal` or a nested model. Constraints the API cannot enforce, such as
`ge=1`, are checked again by Pydantic when the reply is parsed, and a failure costs the run its
one validation retry.

An agent's `schemas.py` is imported by file path, so it cannot import a sibling agent's module.
Reviewers that share the review contract import these models from here and re-export them under
the names their manifest's `input_model` and `output_model` give. The judge consumes `Finding`
when it merges the panel's reviews.

`Ticket` is the part of a work item an agent sees (ADR 0002, `docs/work-items.md`). The store in
`runtime/work_items.py` keeps the rest: id, status, parent, dependencies, size, run ids, events and
the cost, none of which an agent reads or writes.
"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["info", "minor", "major", "blocker"]
TicketKind = Literal["feature", "bug", "refactor", "docs", "research", "incident"]
Verdict = Literal["approve", "request_changes"]


class ReviewInput(BaseModel):
    """What to review: the commits `base` and `head` of the repository at `repo_path`."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools read from.")
    base: str = Field(description="Git ref of the revision the change starts from.")
    head: str = Field(description="Git ref of the revision under review.")
    focus: str | None = Field(
        default=None,
        description="Optional area to look at first, such as 'error handling' or a file path.",
    )


class Finding(BaseModel):
    """One defect or concern, located in the head revision."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="Path relative to the repository root, as it is at head.")
    line_start: int = Field(ge=1, description="First line of the code at fault, in the head file.")
    line_end: int = Field(
        ge=1, description="Last line of the code at fault; equal to line_start for a single line."
    )
    severity: Severity = Field(description="info, minor, major or blocker.")
    category: str = Field(description="Short lowercase label, such as logic, security or testing.")
    title: str = Field(description="One line naming the problem.")
    body: str = Field(description="What is wrong, why it matters, and how it fails.")
    suggested_fix: str | None = Field(
        default=None, description="A concrete fix, when one is clear."
    )

    @model_validator(mode="after")
    def _lines_in_order(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end must not be smaller than line_start")
        return self


class ReviewOutput(BaseModel):
    """The review: every finding, a short summary and an overall verdict."""

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding] = Field(description="Every finding; empty when the change is clean.")
    summary: str = Field(description="Two or three sentences on what the change does and its risk.")
    verdict: Verdict = Field(description="approve, or request_changes when any finding matters.")


class Citation(BaseModel):
    """A source for a claim: the page, a passage copied from it, and when it was read.

    The `citations_resolve` eval check (`evals/graders/citations.py`) fetches `url` and looks for
    `quote` in the page text, so the quote must be copied from the page and not paraphrased.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=1, pattern=r"^https?://", description="http(s) URL of the page that says it."
    )
    quote: str = Field(
        min_length=1,
        description="A passage copied word for word from the page that supports the claim.",
    )
    retrieved_at: datetime = Field(description="When the page was read, as an ISO 8601 timestamp.")


class Claim(BaseModel):
    """One statement of a research output, marked as a fact or not, with its sources.

    This is how an output marks a fact: `fact` is true. A statement of fact, one that could be
    checked against a source, needs at least one citation; an opinion, a recommendation or a
    summary of the agent's own reasoning sets `fact` to false and may cite nothing. A research
    agent's output model holds a list of `Claim` and nothing else needs marking. The
    `citations_resolve` eval check reads exactly these fields: `fact` and `citations`.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, description="The statement, in one or two sentences.")
    fact: bool = Field(
        description="True for a statement of fact that a source can confirm, false for any other."
    )
    citations: list[Citation] = Field(
        description="The sources; at least one when `fact` is true, otherwise may be empty."
    )

    @model_validator(mode="after")
    def _fact_is_cited(self) -> Self:
        if self.fact and not self.citations:
            raise ValueError("a claim marked as a fact needs at least one citation")
        return self


class Ticket(BaseModel):
    """One work item as an agent sees it: what to do, where, and how to tell it is done.

    It carries no id, status, cost or run ids; those stay in the store. `repo` is `owner/name` on
    GitHub or an absolute path to a local checkout. `impacted_paths` are relative to the
    repository root and empty when unknown.
    """

    model_config = ConfigDict(extra="forbid")

    kind: TicketKind = Field(description="What sort of work: it selects the pipeline.")
    title: str = Field(
        min_length=1, max_length=120, description="One line, at most 120 characters."
    )
    body: str = Field(description="Markdown: what to do and why.")
    acceptance: list[str] = Field(
        min_length=1,
        description="Criteria a test or a reader can check, at least one. For research, the question.",
    )
    repo: str = Field(
        description="`owner/name` on GitHub, or an absolute path to a local checkout."
    )
    ref: str = Field(min_length=1, description="The git ref work starts from.")
    impacted_paths: list[str] = Field(
        default_factory=list,
        description="Paths relative to the repository root the work touches; empty when unknown.",
    )

    @field_validator("title")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value or not value.strip():
            raise ValueError("title must be one non-blank line")
        return value

    @field_validator("acceptance")
    @classmethod
    def _criteria(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("acceptance criteria must not be blank")
        return value

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str) -> str:
        if value.startswith("/"):
            return value
        owner, sep, name = value.partition("/")
        if not sep or not owner or not name or "/" in name or " " in value:
            raise ValueError("repo must be owner/name or an absolute path")
        return value

    @field_validator("impacted_paths")
    @classmethod
    def _relative(cls, value: list[str]) -> list[str]:
        if any(not path or path.startswith("/") for path in value):
            raise ValueError("impacted_paths must be relative to the repository root")
        return value
