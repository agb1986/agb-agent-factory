"""The DuckDB trace store: one row per agent run, and the cost queries over those rows.

Connection handling: a `Trace` opens its connection once, on first use, and holds it until
`close()`. DuckDB allows one read-write process per file, so opening per call would pay the file
open and lock on every run and let a second process slip in between two calls.

Single writer (#26): one `Trace` is shared by runs gathered on an event loop and by hooks and
guard runs on worker threads, and a DuckDB connection must not be used from two threads at once.
Every public method therefore holds the instance's lock, so its one connection sees one call at a
time. A lock rather than a writer task and queue: `record` is a synchronous insert of one row that
takes well under a millisecond, a queue would need a flush before every read (`factory cost`, the
budget's cost queries) and a task to start and stop with the loop, and callers on worker threads
have no loop to hand a row to. The lock is reentrant because `record` applies the schema first.

Schema changes are numbered migrations in `MIGRATIONS`. `ensure_schema()` applies the ones not yet
recorded in `schema_version`, each in its own transaction, so calling it twice does nothing the
second time. Never edit a migration that has shipped: append a new one.

Nothing secret goes into a row. Callers pass already-validated payloads in `input_json` and
`output_json`.
"""

import contextlib
import functools
import json
import os
import threading
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Concatenate, Literal, Self

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
    (
        2,
        """
        CREATE TABLE published (
            agent VARCHAR NOT NULL,
            version VARCHAR NOT NULL,
            pass_rate DOUBLE NOT NULL,
            published_at TIMESTAMPTZ NOT NULL,
            git_sha VARCHAR NOT NULL
        )
        """,
    ),
    (3, "ALTER TABLE traces ADD COLUMN parent_run_id VARCHAR"),
    (
        4,
        """
        CREATE TABLE work_items (
            id VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL
                CHECK (kind IN ('feature', 'bug', 'refactor', 'docs', 'research', 'incident')),
            title VARCHAR NOT NULL,
            body VARCHAR NOT NULL,
            acceptance JSON NOT NULL,
            repo VARCHAR NOT NULL,
            ref VARCHAR NOT NULL,
            parent VARCHAR,
            depends_on JSON NOT NULL,
            impacted_paths JSON NOT NULL,
            size VARCHAR CHECK (size IN ('S', 'M', 'L')),
            status VARCHAR NOT NULL CHECK (status IN (
                'todo', 'ready', 'in_progress', 'in_review', 'blocked', 'escalated', 'done',
                'dropped'
            )),
            pipeline_run_ids JSON NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """,
    ),
    (
        5,
        """
        CREATE TABLE work_item_events (
            work_item_id VARCHAR NOT NULL,
            "at" TIMESTAMPTZ NOT NULL,
            from_status VARCHAR,
            to_status VARCHAR NOT NULL,
            actor VARCHAR NOT NULL,
            pipeline_run_id VARCHAR,
            reason VARCHAR NOT NULL CHECK (reason <> '')
        )
        """,
    ),
    (
        6,
        """
        CREATE TABLE escalations (
            id VARCHAR PRIMARY KEY,
            work_item_id VARCHAR NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            actor VARCHAR NOT NULL,
            pipeline_run_id VARCHAR,
            "trigger" VARCHAR NOT NULL CHECK ("trigger" IN (
                'blocked_output', 'budget', 'changes_requested', 'contradiction', 'check_failed'
            )),
            question VARCHAR NOT NULL CHECK (question <> ''),
            from_status VARCHAR NOT NULL,
            pipeline_run_ids JSON NOT NULL,
            cost_usd DOUBLE NOT NULL,
            diff VARCHAR NOT NULL,
            agent_outputs JSON NOT NULL,
            suite_tail VARCHAR NOT NULL,
            answer VARCHAR,
            answer_source VARCHAR,
            answered_at TIMESTAMPTZ
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
    parent_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedRow:
    """One agent version that passed its evals, as written to the `published` table."""

    agent: str
    version: str
    pass_rate: float
    published_at: datetime
    git_sha: str


@dataclass(frozen=True, slots=True)
class CostRow:
    """The runs of one group and tag bucket, as returned by `Trace.cost_breakdown`."""

    group: str | None
    bucket: str
    runs: int
    cost_usd: float


_GROUP_COLUMNS = {
    "agent": "agent_ref",
    "pipeline": "pipeline_run_id",
    # The column says how a run was called; the report names the runner: Claude or Jev.
    "kind": "CASE kind WHEN 'llm' THEN 'claude' ELSE kind END",
}


def resolve_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Return the store path: the argument, else `FACTORY_TRACE_DB`, else `data/traces.duckdb`."""
    if path is not None:
        return Path(path)
    return Path(os.environ.get(ENV_VAR) or DEFAULT_PATH)


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _serialised[**P, R](
    method: Callable[Concatenate[Trace, P], R],
) -> Callable[Concatenate[Trace, P], R]:
    """Run `method` holding the instance's lock, so its connection sees one call at a time."""

    @functools.wraps(method)
    def locked(self: Trace, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class Trace:
    """The trace store. Holds one DuckDB connection per instance; see the module docstring.

    Safe to share between threads and between runs gathered on one event loop.
    """

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
        self._lock = threading.RLock()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @_serialised
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

    @_serialised
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

    @contextlib.contextmanager
    def connection(self) -> Generator[duckdb.DuckDBPyConnection]:
        """Hold the lock and yield the connection, with the schema applied, for the block.

        For a store that lives in the same file, such as `runtime/work_items.py`: its queries run
        under the same lock as `record`, so the one connection still sees one call at a time. The
        block may open its own transaction; it must not close the connection.
        """
        with self._lock:
            self.ensure_schema()
            yield self._connect()

    @_serialised
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

    @_serialised
    def record(self, row: TraceRow) -> None:
        """Write one run. Creates the schema first, so a fresh store needs no setup."""
        if row.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        self.ensure_schema()
        self._connect().execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                row.parent_run_id,
            ],
        )

    @_serialised
    def record_published(
        self,
        agent: str,
        version: str,
        pass_rate: float,
        git_sha: str,
        published_at: datetime | None = None,
    ) -> None:
        """Mark `agent@version` as published: its evals passed at `pass_rate` on commit `git_sha`.

        Appends one row to the `published` table, stamped now (UTC) unless `published_at` is given.
        Publishing a version again adds another row; the table is a history, and a version counts
        as published once it has any row. Creates the schema first.
        """
        stamp = datetime.now(UTC) if published_at is None else published_at
        if stamp.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        self.ensure_schema()
        self._connect().execute(
            "INSERT INTO published VALUES (?, ?, ?, ?, ?)",
            [agent, version, pass_rate, stamp.astimezone(UTC), git_sha],
        )

    @_serialised
    def published(self, agent: str | None = None) -> list[PublishedRow]:
        """Return every `published` row, oldest first, optionally only those of one agent."""
        self.ensure_schema()
        # Fetching a TIMESTAMPTZ makes duckdb import pytz; read it as UTC wall time instead.
        sql = (
            "SELECT agent, version, pass_rate, timezone('UTC', published_at), git_sha"
            " FROM published"
        )
        params: list[Any] = []
        if agent is not None:
            sql += " WHERE agent = ?"
            params.append(agent)
        sql += " ORDER BY published_at, agent, version"
        found = self._connect().execute(sql, params).fetchall()
        return [
            PublishedRow(a, v, rate, at.replace(tzinfo=UTC), sha) for a, v, rate, at, sha in found
        ]

    @_serialised
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

    @_serialised
    def cost_breakdown(
        self, since: datetime, by: Literal["agent", "pipeline", "kind"] = "agent"
    ) -> list[CostRow]:
        """Return runs started at or after `since`, counted and costed per group and tag bucket.

        `by` groups by `agent_ref`, by `pipeline_run_id` (`None` for runs outside a pipeline) or
        by runner kind: `claude` for the `llm` rows and `jev` for the Jev ones. The bucket is `eval` or `publish` for a tag that starts with `eval:` or `publish:`, and
        `other` for any other tag and for no tag. Runs with a null cost add nothing. Rows come
        ordered by group, then bucket. Raises `ValueError` for a naive `since`.
        """
        self.ensure_schema()
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        column = _GROUP_COLUMNS[by]
        sql = (
            f"SELECT {column} AS grp, CASE WHEN tag LIKE 'eval:%' THEN 'eval'"
            " WHEN tag LIKE 'publish:%' THEN 'publish' ELSE 'other' END AS bucket,"
            " count(*), COALESCE(sum(cost_usd), 0.0) FROM traces WHERE started_at >= ?"
            " GROUP BY grp, bucket ORDER BY grp NULLS LAST, bucket"
        )
        found = self._connect().execute(sql, [since.astimezone(UTC)]).fetchall()
        return [CostRow(group, bucket, runs, float(cost)) for group, bucket, runs, cost in found]

    @_serialised
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

    @_serialised
    def pipeline_runs(self, pipeline_run_id: str) -> list[TraceRow]:
        """Return every run of one pipeline invocation, earliest start first."""
        self.ensure_schema()
        found = (
            self._connect()
            .execute(
                "SELECT run_id, agent_ref, kind, epoch_ms(started_at), duration_ms, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, decision_json,"
                " error, tag, parent_run_id FROM traces WHERE pipeline_run_id = ? ORDER BY started_at, run_id",
                [pipeline_run_id],
            )
            .fetchall()
        )
        return [
            TraceRow(
                run_id=run_id,
                agent_ref=agent_ref,
                kind=kind,
                started_at=datetime.fromtimestamp(started_ms / 1000, UTC),
                duration_ms=duration_ms,
                input_json=None,
                pipeline_run_id=pipeline_run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost_usd=cost_usd,
                decision_json=json.loads(decision) if decision is not None else None,
                error=error,
                tag=tag,
                parent_run_id=parent_run_id,
            )
            for (
                run_id,
                agent_ref,
                kind,
                started_ms,
                duration_ms,
                input_tokens,
                output_tokens,
                cache_read,
                cache_write,
                cost_usd,
                decision,
                error,
                tag,
                parent_run_id,
            ) in found
        ]
