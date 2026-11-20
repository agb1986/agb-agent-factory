"""Books and their ISBNs."""


def isbn_ok(isbn: str) -> bool:
    """Return whether the ten-digit `isbn` has a valid check digit; hyphens are ignored."""
    digits = isbn.replace("-", "")
    if len(digits) != 10 or not digits[:9].isdigit():
        return False
    last = 10 if digits[9] in "Xx" else int(digits[9]) if digits[9].isdigit() else -1
    if last < 0:
        return False
    total = sum((10 - i) * int(d) for i, d in enumerate(digits[:9])) + last
    return total % 11 == 0


class Shelf:
    def __init__(self) -> None:
        self._titles: dict[str, str] = {}

    def add(self, isbn: str, title: str) -> None:
        if not isbn_ok(isbn):
            raise ValueError(f"bad ISBN: {isbn}")
        self._titles[isbn.replace("-", "")] = title

    def find(self, isbn: str) -> str | None:
        return self._titles.get(isbn.replace("-", ""))
