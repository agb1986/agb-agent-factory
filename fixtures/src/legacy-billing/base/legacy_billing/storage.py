"""Tables kept as JSON files in the data directory of the run."""

import json
from pathlib import Path
from typing import Any

from legacy_billing import state


def open_store(data_dir: str | Path) -> None:
    """Use `data_dir` for every table from now on."""
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    state.RUN["data_dir"] = str(data_dir)


def _table_path(table: str) -> Path:
    return Path(state.RUN["data_dir"]) / f"{table}.json"


def read_rows(table: str) -> list[dict[str, Any]]:
    """Return every row of `table`, oldest first."""
    path = _table_path(table)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def append_row(table: str, row: dict[str, Any]) -> None:
    """Add one row to the end of `table`."""
    rows = read_rows(table)
    rows.append(row)
    _write_table(table, rows)


def replace_row(table: str, key: str, row: dict[str, Any]) -> None:
    """Replace the row of `table` whose `key` equals that of `row`, or add it."""
    rows = read_rows(table)
    for i, old in enumerate(rows):
        if old[key] == row[key]:
            rows[i] = row
            break
    else:
        rows.append(row)
    _write_table(table, rows)


def _write_table(table: str, rows: list[dict[str, Any]]) -> None:
    _table_path(table).write_text(json.dumps(rows, indent=1), encoding="utf-8")
