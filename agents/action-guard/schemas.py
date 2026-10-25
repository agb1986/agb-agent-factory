"""Input and output models of the action-guard agent.

The guard sees one tool call before it runs, with the root of the worktree the call must stay in and
a short summary of the task, and answers with a decision and the reason for it. `runtime.guard`
builds the input from the tool call and turns the output into a hook decision; the agent has no
tools of its own and never sees the repository.
"""

from pydantic import BaseModel, ConfigDict, Field


class GuardInput(BaseModel):
    """One tool call to judge, and the context needed to judge it."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, description="Dotted name of the tool, such as shell.run.")
    arguments: dict[str, object] = Field(
        description="The arguments the model passed to the tool, exactly as it passed them."
    )
    worktree_root: str = Field(
        min_length=1,
        description="Absolute path of the worktree. A call may touch only paths inside it.",
    )
    task_summary: str = Field(
        description="One or two sentences on what the calling agent was asked to do."
    )


class GuardOutput(BaseModel):
    """The guard's decision on the tool call."""

    model_config = ConfigDict(extra="forbid")

    allow: bool = Field(description="True when the call is safe to run, false when it is refused.")
    reason: str = Field(
        min_length=1,
        description="One or two sentences. For a refusal, the rule broken and how to adjust.",
    )
