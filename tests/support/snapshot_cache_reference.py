"""Deterministic reference for the coherent-snapshot cache boundary.

This module exists ONLY to prove the durability and read-isolation contract of
the ``specification-to-source/backend-application/durable-split-service``
generation skill (ADR 0027, issue #216) is real and implementable. It is a
*test fixture*, not framework runtime: Literate AI does not run caches. A coding
CLI generating the four-boundary composition is what turns this contract into
product code; this reference is the executable acceptance oracle for the cache
boundary's coherent-snapshot property.

Design constraints honored here (from ADR 0027 and issue #216):

- Only the standard library is used: ``sqlite3`` (WAL, busy timeout, read-only
  URI connections) and ``datetime`` (UTC).
- The cache exposes exactly two capabilities: a single-writer *write*
  capability (the collector boundary) and a *read* capability (the API boundary,
  and therefore the frontend). They are distinct classes so a reader can never
  reach a write path.
- Publication is *coherent*: a ``current snapshot`` pointer advances atomically,
  once, only after every required write for a window validates. Readers always
  see the last COMPLETE snapshot; an in-progress or failed/aborted collection
  leaves them on the prior complete snapshot with no partial visibility.
- Reader connections open the same SQLite file in read-only mode
  (``mode=ro`` + ``PRAGMA query_only=ON``) and fail closed on any write.
- The API boundary reads ONLY the cache; it never reaches an upstream source.
  ``ApiReadModel`` is deliberately given no upstream client to make that
  structural, and ``ForbiddenUpstream`` asserts the read path never calls one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class SnapshotCacheError(RuntimeError):
    """A snapshot-cache invariant was violated."""


class ForbiddenUpstream:
    """A stand-in upstream client that must never be called on a read path.

    The read boundaries (API, frontend) are handed nothing like this; it exists
    only so a test can assert that resolving the read path touches no upstream.
    Any attribute access raises, so an accidental upstream call fails loudly.
    """

    def __getattr__(self, name: str) -> object:  # pragma: no cover - defensive
        raise SnapshotCacheError(
            f"read path must not reach upstream (attempted {name!r})"
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A resolved, complete snapshot: its identity, freshness, and rows."""

    snapshot_id: int
    window: str
    published_at: datetime
    rows: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class Freshness:
    """Progress/freshness metadata a read-only reader can query."""

    current_snapshot_id: int | None
    current_window: str | None
    published_at: datetime | None
    collecting_window: str | None


_MIGRATION = """
CREATE TABLE IF NOT EXISTS snapshot (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    window          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    published_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'collecting'
);
CREATE TABLE IF NOT EXISTS snapshot_row (
    snapshot_id     INTEGER NOT NULL,
    metric          TEXT NOT NULL,
    value           REAL NOT NULL,
    PRIMARY KEY (snapshot_id, metric)
);
CREATE TABLE IF NOT EXISTS current_snapshot (
    id              INTEGER PRIMARY KEY CHECK (id = 0),
    snapshot_id     INTEGER NOT NULL
);
"""


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(moment):
        raise SnapshotCacheError("instant must be a timezone-aware UTC instant")
    return moment.isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def migrate(database: Path) -> None:
    """Create the cache schema (explicit migration) with WAL enabled."""

    connection = sqlite3.connect(Path(database), isolation_level=None, timeout=5.0)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(_MIGRATION)
    finally:
        connection.close()


class SnapshotWriter:
    """The single-writer WRITE capability — the collector boundary.

    A collection window is written in a staging snapshot row that readers cannot
    see as "current". Only ``publish`` — after every required write for the
    window is present and validates — advances the ``current_snapshot`` pointer,
    atomically and exactly once. ``abort`` discards a partial window without ever
    touching the pointer, so readers stay on the prior complete snapshot.
    """

    def __init__(self, database: Path) -> None:
        self._database = Path(database)
        self._connection = sqlite3.connect(
            self._database, isolation_level=None, timeout=5.0
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(_MIGRATION)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SnapshotWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def begin_snapshot(self, *, window: str, now: datetime) -> int:
        """Open a staging snapshot for a collection window.

        The snapshot is ``collecting`` and invisible to readers until published.
        """

        cursor = self._connection.execute(
            "INSERT INTO snapshot (window, started_at, status) VALUES (?, ?, ?)",
            (window, _iso(now), "collecting"),
        )
        return int(cursor.lastrowid or 0)

    def write_row(self, *, snapshot_id: int, metric: str, value: float) -> None:
        """Write one required row into the staging snapshot (not yet visible)."""

        self._connection.execute(
            "INSERT INTO snapshot_row (snapshot_id, metric, value) VALUES (?, ?, ?)",
            (snapshot_id, metric, float(value)),
        )

    def publish(
        self,
        *,
        snapshot_id: int,
        required_metrics: Mapping[str, object] | tuple[str, ...],
        now: datetime,
    ) -> None:
        """Validate the window's writes and advance the current pointer atomically.

        Publication is coherent: it verifies every required metric was written,
        then flips the snapshot to ``published`` and repoints ``current_snapshot``
        in ONE immediate transaction. If validation fails nothing is published and
        readers stay on the prior complete snapshot.
        """

        required = tuple(required_metrics)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            present = {
                row[0]
                for row in self._connection.execute(
                    "SELECT metric FROM snapshot_row WHERE snapshot_id = ?",
                    (snapshot_id,),
                )
            }
            missing = [metric for metric in required if metric not in present]
            if missing:
                self._connection.execute("ROLLBACK")
                raise SnapshotCacheError(
                    f"cannot publish snapshot {snapshot_id}: missing {missing}"
                )
            self._connection.execute(
                "UPDATE snapshot SET status = 'published', published_at = ? "
                "WHERE snapshot_id = ?",
                (_iso(now), snapshot_id),
            )
            self._connection.execute(
                "INSERT INTO current_snapshot (id, snapshot_id) VALUES (0, ?) "
                "ON CONFLICT(id) DO UPDATE SET snapshot_id = excluded.snapshot_id",
                (snapshot_id,),
            )
            self._connection.execute("COMMIT")
        except SnapshotCacheError:
            raise
        except sqlite3.Error:  # pragma: no cover - defensive
            self._connection.execute("ROLLBACK")
            raise

    def abort(self, *, snapshot_id: int) -> None:
        """Discard a partial/failed staging snapshot without moving the pointer."""

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "DELETE FROM snapshot_row WHERE snapshot_id = ?", (snapshot_id,)
            )
            self._connection.execute(
                "UPDATE snapshot SET status = 'aborted' WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            self._connection.execute("COMMIT")
        except sqlite3.Error:  # pragma: no cover - defensive
            self._connection.execute("ROLLBACK")
            raise


class SnapshotReader:
    """The READ capability — a read-only connection for the API boundary.

    Opens the SAME SQLite file in read-only mode and enforces ``query_only``, so
    any write attempt fails closed. It resolves the ``current_snapshot`` pointer
    and returns only that complete snapshot; it can never observe a ``collecting``
    or ``aborted`` snapshot's rows as current.
    """

    def __init__(self, database: Path) -> None:
        self._database = Path(database)
        uri = f"file:{self._database}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        self._connection.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SnapshotReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _current_id(self) -> int | None:
        row = self._connection.execute(
            "SELECT snapshot_id FROM current_snapshot WHERE id = 0"
        ).fetchone()
        return int(row[0]) if row is not None else None

    def current_snapshot(self) -> Snapshot | None:
        """Return the last COMPLETE snapshot, or None before any publish."""

        current = self._current_id()
        if current is None:
            return None
        meta = self._connection.execute(
            "SELECT window, published_at, status FROM snapshot WHERE snapshot_id = ?",
            (current,),
        ).fetchone()
        if meta is None or meta[2] != "published":  # pragma: no cover - defensive
            raise SnapshotCacheError("current pointer names a non-published snapshot")
        rows = tuple(
            (str(metric), float(value))
            for metric, value in self._connection.execute(
                "SELECT metric, value FROM snapshot_row WHERE snapshot_id = ? "
                "ORDER BY metric",
                (current,),
            )
        )
        return Snapshot(
            snapshot_id=current,
            window=str(meta[0]),
            published_at=_parse(str(meta[1])),
            rows=rows,
        )

    def freshness(self) -> Freshness:
        """Progress/freshness metadata visible to a read-only reader."""

        current = self._current_id()
        current_window: str | None = None
        published_at: datetime | None = None
        if current is not None:
            meta = self._connection.execute(
                "SELECT window, published_at FROM snapshot WHERE snapshot_id = ?",
                (current,),
            ).fetchone()
            if meta is not None:
                current_window = str(meta[0])
                published_at = _parse(str(meta[1])) if meta[1] else None
        collecting = self._connection.execute(
            "SELECT window FROM snapshot WHERE status = 'collecting' "
            "ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        return Freshness(
            current_snapshot_id=current,
            current_window=current_window,
            published_at=published_at,
            collecting_window=str(collecting[0]) if collecting is not None else None,
        )

    def try_write(self) -> None:
        """Attempt a write; the read-only connection MUST reject it (fail closed)."""

        self._connection.execute(
            "INSERT INTO snapshot (window, started_at) VALUES ('x', 'x')"
        )


class ApiReadModel:
    """The read-only API boundary: reads ONLY the cache, never upstream.

    Given no upstream client (only an inert ``ForbiddenUpstream`` sentinel), so
    the four-boundary read path frontend->API->cache is structurally incapable of
    reaching an upstream source. This models boundary 2 of ADR 0027.
    """

    def __init__(self, database: Path) -> None:
        self._database = Path(database)
        self.upstream = ForbiddenUpstream()

    def read_current(self) -> Snapshot | None:
        with SnapshotReader(self._database) as reader:
            return reader.current_snapshot()

    def read_freshness(self) -> Freshness:
        with SnapshotReader(self._database) as reader:
            return reader.freshness()
