"""State shared by the whole package. Every module may read it and most of them write it."""

from collections.abc import Callable
from typing import Any

RUN: dict[str, Any] = {}
CACHE: dict[str, dict[str, Any]] = {}
COUNTERS: dict[str, int] = {}
WARNINGS: list[str] = []
HOOKS: list[Callable[[dict[str, Any]], None]] = []


def reset() -> None:
    """Forget everything, as if the process had just started."""
    RUN.clear()
    CACHE.clear()
    COUNTERS.clear()
    WARNINGS.clear()
    HOOKS.clear()
