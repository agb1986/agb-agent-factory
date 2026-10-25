from pathlib import Path

from uploads.storage import read, save


def test_save_keeps_only_the_file_name(tmp_path: Path) -> None:
    target = save(str(tmp_path), "../escape.txt", b"x")
    assert target == tmp_path / "escape.txt"
    assert target.read_bytes() == b"x"


def test_read_returns_what_was_saved(tmp_path: Path) -> None:
    save(str(tmp_path), "a.txt", b"hello")
    assert read(str(tmp_path), "a.txt") == b"hello"
