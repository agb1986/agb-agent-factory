"""Live end-to-end tests of the fixer prototype on the `off-by-one` fixture.

Marked `live`, and skipped unless `ANTHROPIC_API_KEY` is set and Docker has the sandbox image.
The option (b) test also needs the Agent SDK, which is not a project dependency, so it runs only
under `uv run --with claude-agent-sdk==0.2.174 pytest prototypes/fixer -m live`. Each fix costs
a few cents. This directory has no `conftest.py`, so the key check is made here.
"""

import os
from pathlib import Path
from typing import Any

import anthropic
import pytest

from prototypes.fixer import run
from prototypes.fixer.sandbox import require_image
from prototypes.fixer.test_fixer_sandbox import _sandbox_missing
from registry.resolver import load_spec

_MISSING = None if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY not set"
_MISSING = _MISSING or _sandbox_missing()

pytestmark = [pytest.mark.live, pytest.mark.skipif(_MISSING is not None, reason=_MISSING or "")]


def test_the_manifest_model_is_served() -> None:
    model = load_spec(run.MANIFEST).model
    assert model in {m.id for m in anthropic.Anthropic().models.list()}


def _assert_fixed(result: dict[str, Any]) -> None:
    assert result["error"] is None
    assert result["fixed"], result["checks"]
    assert result["factory_tree_unchanged"]
    assert "series/rolling.py" in result["files_changed"]
    assert Path(result["patch"]).read_text().startswith("diff --git ")


def test_option_a_fixes_the_fixture(tmp_path: Path) -> None:
    require_image()
    _assert_fixed(run.fix(tmp_path))


def test_option_b_fixes_the_fixture(tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    from prototypes.fixer import sdk_run

    _assert_fixed(sdk_run.fix(tmp_path))
