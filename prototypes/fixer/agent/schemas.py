"""Input and output models of the fixer prototype (`prototypes/fixer/agent/agent.yaml`)."""

from pydantic import BaseModel, ConfigDict, Field


class Finding(BaseModel):
    """The defect to fix, as the code reviewer reported it."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="Path of the faulty file, relative to the repository root.")
    line_start: int = Field(ge=1, description="First line of the faulty code.")
    line_end: int = Field(ge=1, description="Last line of the faulty code.")
    title: str = Field(description="One-line name of the defect.")
    description: str = Field(description="What goes wrong, with an example input if known.")


class FixInput(BaseModel):
    """What to fix: one finding. The tools already act on the run's repository, so no path."""

    model_config = ConfigDict(extra="forbid")

    finding: Finding


class FixReport(BaseModel):
    """What the fixer did."""

    model_config = ConfigDict(extra="forbid")

    files_changed: list[str] = Field(
        description="Every file changed or created, relative to the repository root."
    )
    commands_run: list[str] = Field(description="Every shell.run command, in the order run.")
    summary: str = Field(description="The cause of the defect and how the change fixes it.")
