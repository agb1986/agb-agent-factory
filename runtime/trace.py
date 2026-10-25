"""The DuckDB trace store: one row per agent run, and the cost queries over those rows.

Connection handling: a `Trace` opens its connection once, on first use, and holds it until
`close()`. DuckDB allows one read-write process per file, so opening per call would pay the file
open and lock on every run and let a second process slip in between two calls. Holding the
connection is also what lets the single writer in #26 own the file. A `Trace` is not thread-safe;
give each thread its own `Trace` on a shared path only when they live in one process, because
DuckDB shares the database between connections of one process.

Schema changes are numbered migrations in `MIGRATIONS`. `ensure_schema()` applies the ones not yet
recorded in `schema_version`, each in its own transaction, so calling it twice does nothing the
second time. Never edit a migration that has shipped: append a new one.

Nothing secret goes into a row. Callers pass already-validated payloads in `input_json` and
`output_json`.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb

DEFAULT_PATH = Path("data/traces.duckdb")
ENV_VAR = "FACTORY_TRACE_DB"

Migration = tuple[int, str]

MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        """
        CREATE TABLE traces (
            run_id VARCHAR PRIMARY KEY,
            pipeline_run_id VARCHAR,
            agent_ref VARCHAR NOT NULL,
            kind VARCHAR NOT NULL CHECK (kind IN ('llm', 'jev')),
            started_at TIMESTAMPTZ NOT NULL,
            duration_ms BIGINT NOT NULL,
            input_tokens BIGINT,
            output_tokens BIGINT,
            cache_read_tokens BIGINT,
            cache_write_tokens BIGINT,
            cost_usd DOUBLE,
            decision_json JSON,
            input_json JSON NOT NULL,
            output_json JSON,
            error VARCHAR,
            tag VARCHAR
        )
        """,
    ),
)


@dataclass(frozen=True, slots=True)
class TraceRow:
    """One agent run, as written to the `traces` table. See "Trace row" in docs/CONVENTIONS.md."""

    run_id: str
    agent_ref: str
    kind: str
    started_at: datetime
    duration_ms: int
    input_json: Any
    pipeline_run_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    decision_json: Any = None
    output_json: Any = None
    error: str | None = None
    tag: str | None = None


def resolve_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Return the store path: the argument, else `FACTORY_TRACE_DB`, else `data/traces.duckdb`."""
    if path is not None:
        return Path(path)
    return Path(os.environ.get(ENV_VAR) or DEFAULT_PATH)


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


class Trace:
    """The trace store. Holds one DuckDB connection per instance; see the module docstring."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        migrations: Sequence[Migration] = MIGRATIONS,
    ) -> None:
        """Remember where the store lives. Nothing is opened until the first call that needs it."""
        self.path = resolve_path(path)
        self._migrations = tuple(sorted(migrations))
        self._con: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection. A later call reopens it."""
        if self._con is not None:
            self._con.close()
            self._con = None

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._con = duckdb.connect(str(self.path))
            self._con.execute("SET TimeZone = 'UTC'")
        return self._con

    def ensure_schema(self) -> None:
        """Create `schema_version` and apply every migration not yet recorded. Safe to repeat."""
        con = self._connect()
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
        )
        row = con.execute("SELECT max(version) FROM schema_version").fetchone()
        current = row[0] if row and row[0] is not None else 0
        for version, sql in self._migrations:
            if version <= current:
                continue
            con.execute("BEGIN")
            try:
                con.execute(sql)
                con.execute(
                    "INSERT INTO schema_version VALUES (?, ?)", [version, datetime.now(UTC)]
                )
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    def schema_version(self) -> int:
        """Return the highest applied migration number, or 0 for a store with none."""
        con = self._connect()
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'schema_version'"
        ).fetchone()
        if not exists or not exists[0]:
            return 0
        row = con.execute("SELECT max(version) FROM schema_version").fetchone()
        return row[0] if row and row[0] is not None else 0

    def record(self, row: TraceRow) -> None:
        """Write one run. Creates the schema first, so a fresh store needs no setup."""
        if row.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        self.ensure_schema()
        self._connect().execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                row.run_id,
                row.pipeline_run_id,
                row.agent_ref,
                row.kind,
                row.started_at.astimezone(UTC),
                row.duration_ms,
                row.input_tokens,
                row.output_tokens,
                row.cache_read_tokens,
                row.cache_write_tokens,
                row.cost_usd,
                _json_or_none(row.decision_json),
                json.dumps(row.input_json),
                _json_or_none(row.output_json),
                row.error,
                row.tag,
            ],
        )

    def cost_by_agent(self, since: datetime | None = None) -> dict[str, float]:
        """Return total `cost_usd` per `agent_ref`, optionally for runs started at or after `since`.

        Runs with a null cost add nothing, and an agent whose runs are all unpriced reports 0.0.
        """
        self.ensure_schema()
        sql = "SELECT agent_ref, COALESCE(sum(cost_usd), 0.0) FROM traces"
        params: list[Any] = []
        if since is not None:
            if since.tzinfo is None:
                raise ValueError("since must be timezone-aware")
            sql += " WHERE started_at >= ?"
            params.append(since.astimezone(UTC))
        sql += " GROUP BY agent_ref ORDER BY agent_ref"
        return {ref: float(cost) for ref, cost in self._connect().execute(sql, params).fetchall()}

    def cost_by_pipeline_run(self, pipeline_run_id: str) -> float:
        """Return total `cost_usd` of every run in one pipeline invocation, 0.0 when there is none."""
        self.ensure_schema()
        row = (
            self._connect()
            .execute(
                "SELECT COALESCE(sum(cost_usd), 0.0) FROM traces WHERE pipeline_run_id = ?",
                [pipeline_run_id],
            )
            .fetchone()
        )
        return float(row[0]) if row else 0.0
