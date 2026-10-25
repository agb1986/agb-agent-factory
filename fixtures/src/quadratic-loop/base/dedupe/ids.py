"""Id handling for uploaded batches."""


def normalise_ids(raw: list[str]) -> list[str]:
    """Strip whitespace and lowercase every id, dropping blanks."""
    return [item.strip().lower() for item in raw if item.strip()]


def unique_ids(ids: list[str]) -> list[str]:
    """Return the ids without repeats, keeping the first occurrence of each."""
    return list(dict.fromkeys(ids))
