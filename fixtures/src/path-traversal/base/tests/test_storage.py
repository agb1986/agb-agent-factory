from pathlib import Path

from uploads.storage import save


def test_save_keeps_only_the_file_name(tmp_path: Path) -> None:
    target = save(str(tmp_path), "../escape.txt", b"x")
    assert target == tmp_path / "escape.txt"
    assert target.read_bytes() == b"x"
