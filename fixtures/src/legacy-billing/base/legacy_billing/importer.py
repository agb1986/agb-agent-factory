"""Reads usage files from the meters."""

import csv
from pathlib import Path

from legacy_billing import catalog, state, usage


def _pad(text: str, width: int) -> str:
    return text[:width].ljust(width)


def import_usage(path: str | Path) -> int:
    """Save every reading in a CSV file and return how many there were.

    The columns are customer, day, meter and quantity. A row for an unknown meter is a warning.
    """
    count = 0
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["meter"] not in catalog.METERS:
                state.WARNINGS.append(f"unknown meter {row['meter']}")
                continue
            usage.record_usage(row["customer"], row["day"], row["meter"], int(row["quantity"]))
            count += 1
    return count
