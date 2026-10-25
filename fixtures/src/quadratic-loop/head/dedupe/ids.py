"""Id handling for uploaded batches."""


def normalise_ids(raw: list[str]) -> list[str]:
    """Strip whitespace and lowercase every id, dropping blanks."""
    return [item.strip().lower() for item in raw if item.strip()]


def unique_ids(ids: list[str]) -> list[str]:
    """Return the ids without repeats, keeping the first occurrence of each."""
    return list(dict.fromkeys(ids))


def duplicate_ids(ids: list[str]) -> list[str]:
    """Return each id that appears more than once, in order of its first occurrence.

    The importer calls this once for every uploaded batch, and a batch holds up to 200,000 ids.
    """
    duplicates: list[str] = []
    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            if first == second and first not in duplicates:
                duplicates.append(first)
    return duplicates
