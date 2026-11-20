"""Read a settings file."""

from pathlib import Path


def read_config(path: str | Path) -> dict[str, str]:
    """Return the `key=value` pairs of the file at `path`, skipping blanks and `#` comments."""
    pairs: dict[str, str] = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(f"line {number} has no '='")
        key, value = stripped.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs
