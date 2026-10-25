"""Upload storage on local disk."""

from pathlib import Path


def save(root: str, filename: str, data: bytes) -> Path:
    """Write `data` under `root` using only the last part of `filename`; return the path."""
    target = Path(root) / Path(filename).name
    target.write_bytes(data)
    return target
