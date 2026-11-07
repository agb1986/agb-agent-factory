"""Input and output models of the product-architect agent.

The agent turns an idea, or a piece of raw user feedback, into a product requirements document for
the repository it would be built in. The output holds only what the request states or the
repository shows; whatever a person must still decide goes to `open_questions`.
"""

from pydantic import BaseModel, ConfigDict, Field


class PRDInput(BaseModel):
    """The idea or feedback to write a PRD for, and the repository it concerns."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    ref: str = Field(min_length=1, description="The git ref the PRD is written against.")
    idea: str = Field(
        min_length=1, description="The idea or the raw feedback, as the person wrote it."
    )
    feedback: list[str] = Field(
        default_factory=list,
        description="Earlier feedback on the same subject, one item per entry, oldest first.",
    )


class UserStory(BaseModel):
    """One high-level user story: who wants what, and why."""

    model_config = ConfigDict(extra="forbid")

    as_a: str = Field(description="The kind of user, such as 'a customer paying an invoice'.")
    i_want: str = Field(description="What that user wants to be able to do.")
    so_that: str = Field(description="Why, in the user's terms, as the request states it.")


class OpenQuestion(BaseModel):
    """A decision a person must make before anyone builds the feature."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(description="The question, answerable by the person who asked.")
    why_it_matters: str = Field(description="What the answer changes in what gets built.")


class PRDOutput(BaseModel):
    """A product requirements document. Lists may be empty; nothing in them is invented."""

    model_config = ConfigDict(extra="forbid")

    problem: str = Field(
        min_length=1, description="The problem the request describes, in two to four sentences."
    )
    goals: list[str] = Field(
        description="Outcomes the request asks for. Empty when it contradicts itself on its core ask."
    )
    non_goals: list[str] = Field(
        description="What the request rules out, or what is left out of this scope on purpose."
    )
    user_stories: list[UserStory] = Field(description="High-level stories, one per goal at most.")
    constraints: list[str] = Field(
        description="Limits the request states or the repository imposes, each with its source."
    )
    risks: list[str] = Field(description="What could go wrong in building or shipping it.")
    open_questions: list[OpenQuestion] = Field(
        description="Decisions a person must make before building, including every unknown."
    )
    related_files: list[str] = Field(
        description="Repository paths, relative to the root, that the constraints and risks rest on."
    )


__all__ = ["OpenQuestion", "PRDInput", "PRDOutput", "UserStory"]
