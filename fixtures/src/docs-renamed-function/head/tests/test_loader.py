from pathlib import Path

import pytest
from settings.loader import read_config


def test_load_reads_pairs_and_skips_comments(tmp_path: Path) -> None:
    path = tmp_path / "app.conf"
    path.write_text("# app\n\nname = shop\nport=8080\n", encoding="utf-8")
    assert read_config(path) == {"name": "shop", "port": "8080"}


def test_load_names_the_line_with_no_equals(tmp_path: Path) -> None:
    path = tmp_path / "app.conf"
    path.write_text("name=shop\nport\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        read_config(path)
