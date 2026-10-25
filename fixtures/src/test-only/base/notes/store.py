"""In-memory note storage."""


class NoteStore:
    """Keeps notes in insertion order."""

    def __init__(self) -> None:
        self._notes: list[str] = []

    def add(self, text: str) -> int:
        """Store `text` and return its index."""
        self._notes.append(text)
        return len(self._notes) - 1

    def all(self) -> list[str]:
        """Return a copy of every note, oldest first."""
        return list(self._notes)
