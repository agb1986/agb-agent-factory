"""Split text into words."""

import re

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """Return the lowercase runs of letters and digits in `text`, in order."""
    return _WORD.findall(text.lower())


def slugify(text: str, sep: str = "-") -> str:
    found = words(text)
    if not found:
        raise ValueError("no words to make a slug from")
    return sep.join(found)
