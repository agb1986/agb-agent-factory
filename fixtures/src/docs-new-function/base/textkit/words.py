"""Split text into words."""

import re

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """Return the lowercase runs of letters and digits in `text`, in order."""
    return _WORD.findall(text.lower())
