"""The issue record."""

from dataclasses import dataclass, field


@dataclass
class Issue:
    """One issue: a number, a title, a state and free-form labels."""

    number: int
    title: str
    state: str = "open"
    labels: list[str] = field(default_factory=list)
