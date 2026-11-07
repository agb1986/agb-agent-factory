"""A live fetch through the real `agb`, against a stable public page.

Runs only with `FACTORY_LIVE=1`; it needs network access and `agb` and `defuddle` (which `agb web fetch` runs) on PATH, and no API key.
"""

import os
import shutil
from pathlib import Path

import pytest

from tools import web

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_LIVE") != "1"
    or shutil.which("agb") is None
    or shutil.which("defuddle") is None,
    reason="needs FACTORY_LIVE=1, agb and defuddle (npm install -g defuddle) on PATH",
)


def test_fetch_example_com_through_agb(tmp_path: Path) -> None:
    web.reset_cache()
    web.check_web_call("web.fetch", {"url": "https://example.com/"}, ["example.com"])
    text = web.fetch(tmp_path, "https://example.com/")
    assert "Example Domain" in text
    assert "<<<BEGIN FETCHED CONTENT>>>" in text and text.endswith("<<<END FETCHED CONTENT>>>")
