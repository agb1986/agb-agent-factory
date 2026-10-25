"""Fixtures that build small `agents/` trees on disk."""

from collections.abc import Callable
from pathlib import Path

import pytest

SCHEMAS = """\
from pydantic import BaseModel


class In(BaseModel):
    diff: str


class Out(BaseModel):
    ok: bool


NotAModel = 3
"""

MANIFEST = """\
name: {name}
version: {version}
kind: llm
description: Test agent.
model: claude-sonnet-5-5
effort: high
system_prompt: system.md
schemas: schemas.py
input_model: In
output_model: Out
tools:
  allow: [git.read_file]
  deny: []
limits:
  max_tool_turns: 4
  timeout_s: 60
  max_output_tokens: 1024
evals:
  min_pass_rate: 0.75
"""


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """An empty `agents/` directory."""
    path = tmp_path / "agents"
    path.mkdir()
    return path


@pytest.fixture
def make_agent(agents_dir: Path) -> Callable[..., Path]:
    """Write an LLM agent and return its manifest path.

    `subdir=True` puts it at `agents/<name>/<version>/agent.yaml`. `manifest` replaces the whole
    manifest text and `schemas` the schemas module; `edit` is applied to the default manifest.
    """

    def make(
        name: str,
        version: str,
        *,
        subdir: bool = False,
        manifest: str | None = None,
        schemas: str = SCHEMAS,
        edit: Callable[[str], str] = lambda text: text,
    ) -> Path:
        directory = agents_dir / name / version if subdir else agents_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        text = (
            manifest if manifest is not None else edit(MANIFEST.format(name=name, version=version))
        )
        (directory / "agent.yaml").write_text(text)
        (directory / "system.md").write_text("You review diffs.\n")
        (directory / "schemas.py").write_text(schemas)
        return directory / "agent.yaml"

    return make
