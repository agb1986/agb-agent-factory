"""Input and output models of the story-breakdown agent.

The agent turns a product requirements document into an ordered list of tickets. Each ticket is a
`Ticket` (no id) plus a size and the indexes of the tickets it depends on. The agent writes nothing
to the work-item store; the plan loop turns the indexes into `wi-<n>` ids when it files the tickets
(ADR 0002).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from registry.common_schemas import Ticket

# The same three sizes as `runtime.work_items.SIZES`; a test keeps the two equal. The schemas file
# is loaded by path outside the package, so it does not import the runtime.
Size = Literal["S", "M", "L"]


class StoryInput(BaseModel):
    """The PRD to break down and the repository the tickets are for."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(description="Path of the repository the tools act on.")
    prd: str = Field(min_length=1, description="The product requirements document, as written.")
    repository: str = Field(
        min_length=1,
        description="`owner/name` or an absolute path: copied into every ticket's `repo`.",
    )
    ref: str = Field(
        min_length=1,
        description="The git ref the PRD is broken down against: copied into every ticket's `ref`.",
    )


class PlannedTicket(Ticket):
    """A ticket as the breakdown plans it: the `Ticket` fields, a size and its dependencies."""

    size: Size = Field(
        description="S is a few lines in one place, M a feature in one module, L several modules."
    )
    depends_on: list[int] = Field(
        description="Zero-based indexes, into `tickets`, of the tickets that must be done first."
    )

    @field_validator("impacted_paths")
    @classmethod
    def _repository_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if ".." in path.split("/") or "\\" in path:
                raise ValueError(f"impacted path {path!r} must be a relative POSIX path")
        if len(set(value)) != len(value):
            raise ValueError("impacted_paths must not repeat a path")
        return value

    def as_ticket(self) -> Ticket:
        """The plain `Ticket` the plan loop files, without the planning fields."""
        return Ticket.model_validate(self.model_dump(exclude={"size", "depends_on"}))


class StoryOutput(BaseModel):
    """The tickets for one PRD, in build order, and whatever the PRD left undecided."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, description="One or two sentences on how the work is cut.")
    tickets: list[PlannedTicket] = Field(
        min_length=1, description="The tickets in build order: a ticket follows all it depends on."
    )
    open_questions: list[str] = Field(
        description="What the PRD leaves undecided and the tickets assume; empty when nothing."
    )

    @model_validator(mode="after")
    def _graph_is_a_forward_acyclic_one(self) -> StoryOutput:
        count = len(self.tickets)
        for index, ticket in enumerate(self.tickets):
            deps = ticket.depends_on
            if len(set(deps)) != len(deps):
                raise ValueError(f"ticket {index} lists a dependency twice")
            for dep in deps:
                if not 0 <= dep < count:
                    raise ValueError(f"ticket {index} depends on {dep}, which is not a ticket")
                if dep == index:
                    raise ValueError(f"ticket {index} depends on itself")
        cycle = self._find_cycle()
        if cycle:
            raise ValueError("dependency cycle: " + " -> ".join(str(i) for i in cycle))
        for index, ticket in enumerate(self.tickets):
            for dep in ticket.depends_on:
                if dep > index:
                    raise ValueError(
                        f"ticket {index} depends on {dep}, which comes later: "
                        "list the tickets in build order"
                    )
        return self

    def _find_cycle(self) -> list[int]:
        """One dependency cycle as a closed path of indexes, or an empty list."""
        state: dict[int, int] = {}  # 1 while on the walk, 2 once finished
        walk: list[int] = []

        def visit(node: int) -> list[int]:
            state[node] = 1
            walk.append(node)
            for dep in self.tickets[node].depends_on:
                if state.get(dep) == 1:
                    return [*walk[walk.index(dep) :], dep]
                if dep not in state and (found := visit(dep)):
                    return found
            walk.pop()
            state[node] = 2
            return []

        for node in range(len(self.tickets)):
            if node not in state and (found := visit(node)):
                return found
        return []

    def ancestors(self, index: int) -> set[int]:
        """Every ticket `index` depends on, directly or through others."""
        seen: set[int] = set()
        stack = list(self.tickets[index].depends_on)
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(self.tickets[node].depends_on)
        return seen

    def parallel_conflicts(self) -> list[tuple[int, int, list[str]]]:
        """Pairs of tickets that could run together but share impacted paths.

        Two tickets may run in parallel when neither depends on the other, directly or through
        others. They are safe to run together only if their impacted paths are disjoint and both
        are non-empty. Each result is `(earlier, later, shared paths)`; a pair with an empty list
        has no paths to compare. An empty result means every unordered pair is either ordered by
        a dependency or has disjoint, non-empty paths.
        """
        ancestors = [self.ancestors(index) for index in range(len(self.tickets))]
        found: list[tuple[int, int, list[str]]] = []
        for later in range(len(self.tickets)):
            for earlier in range(later):
                if earlier in ancestors[later] or later in ancestors[earlier]:
                    continue
                first = self.tickets[earlier].impacted_paths
                second = self.tickets[later].impacted_paths
                shared = sorted(set(first) & set(second))
                if shared or not first or not second:
                    found.append((earlier, later, shared))
        return found
