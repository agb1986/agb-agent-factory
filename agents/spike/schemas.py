"""Input and output models of the spike agent.

A spike answers one question about an API or a package by trying it. The agent writes throwaway
code in a scratch directory, runs it in the networked spike sandbox, and reports what it saw: the
shapes of the payloads that came back, the edge cases, a script that works, and the attempts that
failed. Every observation from code carries the command that was run and the output it printed, so
a reader can run it again. Facts read from documentation are claims
(`registry.common_schemas.Claim`), each fact cited with a passage copied from its page.

There is no `repo_path`: a spike never sees a source repository, and the runtime gives it an empty
scratch directory that is deleted when the run ends.
"""

from pydantic import BaseModel, ConfigDict, Field

from registry.common_schemas import Citation, Claim


class SpikeInput(BaseModel):
    """The question to answer and the API or package to try."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, description="What the spike must find out, as asked.")
    target: str = Field(
        min_length=1,
        description="The API base URL or the package name, with a version when one matters.",
    )
    context: str | None = Field(
        default=None,
        description="Optional background: why the question is asked, or what is already known.",
    )


class Observation(BaseModel):
    """One thing learned by running code: the command, what it printed, and what that shows."""

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(min_length=1, description="The argv passed to shell.run.")
    exit_code: int = Field(description="The exit code the command returned.")
    output: str = Field(
        min_length=1,
        description="The part of the output that shows the finding, copied as it was printed.",
    )
    finding: str = Field(
        min_length=1, description="What the output shows, in one or two sentences."
    )


class PayloadShape(BaseModel):
    """The shape of one response or object, as seen in the output of a command."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="What the payload is, such as GET /posts/1.")
    shape: str = Field(
        min_length=1,
        description="Its fields and their types, such as {id: int, title: str, tags: list[str]}.",
    )


class WorkingCode(BaseModel):
    """A script in the scratch directory that ran and showed the answer."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Its path in the scratch directory.")
    content: str = Field(min_length=1, description="Its full content.")


class SpikeOutput(BaseModel):
    """What the spike found: shapes, edge cases, working code, failures and cited facts."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, description="Two or three sentences answering the question.")
    observations: list[Observation] = Field(
        min_length=1, description="Every result from code the answer rests on, as it was run."
    )
    payload_shapes: list[PayloadShape] = Field(
        description="The shapes of the payloads seen; empty when the question is not about data."
    )
    edge_cases: list[str] = Field(
        description=(
            "Behaviour at the edges, such as errors, empty results or limits, each naming the "
            "observation or the claim it comes from."
        )
    )
    working_code: WorkingCode | None = Field(
        description="A script that ran and shows the answer, or null when nothing worked."
    )
    failures: list[str] = Field(
        description="What was tried and did not work, and why; empty when nothing failed."
    )
    claims: list[Claim] = Field(
        description="Facts read from documentation; every fact cites a passage copied from its page."
    )


__all__ = [
    "Citation",
    "Claim",
    "Observation",
    "PayloadShape",
    "SpikeInput",
    "SpikeOutput",
    "WorkingCode",
]
