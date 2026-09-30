"""Deterministic reference scheduler + single-writer lease.

This module exists ONLY to prove the behavioral contract described by the
``specification-to-source/backend-application/scheduler-lease-worker`` generation
skill is real and implementable. It is a *test fixture*, not framework runtime:
Literate AI does not run schedules. A coding CLI generating a worker Component is
what turns this contract into product code; this reference is the executable
acceptance oracle for that contract.

Design constraints honored here (from ADR 0027 and issue #217):

- Only the standard library is used: ``sqlite3``, ``datetime`` (UTC), ``hashlib``.
- The clock and the randomness are *injected* (``clock`` and ``rng``), so every
  temporal and jitter decision is deterministic under test.
- Schedule state AND the lease live inside the SAME embedded SQLite cache
  boundary (WAL, busy timeout, explicit migration), because the single-writer
  lease and the single-writer cache write are one concurrency invariant.
- Restart is expressed as *constructing a new scheduler over the same database
  file* — nothing is retained in process memory across a "restart".
- Retry/backoff state is tracked separately from the next daily schedule.
- Run records and per-source outcome records are append-only.
- Progress/next-run state is readable from a *separate read-only connection*,
  simulating the read-only API process.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from random import Random

Clock = Callable[[], datetime]
"""A zero-argument callable returning the current timezone-aware UTC instant."""


class SchedulerLeaseError(RuntimeError):
    """A scheduler/lease invariant was violated."""


def _require_utc(moment: datetime, label: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise SchedulerLeaseError(f"{label} must be a timezone-aware UTC instant")
    return moment


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Cadence configuration for a once-daily jittered worker.

    ``nominal_run_time`` is the wall-clock UTC time of day the worker nominally
    runs. ``jitter`` is the *symmetric* bound: the selected instant lands in
    ``[nominal - jitter, nominal + jitter]``. ``lease_ttl`` bounds how long a
    single acquired lease is considered live before another worker may recover
    it. ``max_retries`` and ``retry_backoff`` bound in-window retry, tracked
    separately from the next daily schedule.
    """

    nominal_run_time: time = time(3, 0, 0)
    jitter: timedelta = timedelta(minutes=30)
    lease_ttl: timedelta = timedelta(minutes=15)
    max_retries: int = 3
    retry_backoff: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.nominal_run_time.tzinfo is not None:
            raise SchedulerLeaseError(
                "nominal_run_time must be a naive wall-clock time"
            )
        if self.jitter < timedelta(0):
            raise SchedulerLeaseError("jitter must be non-negative")
        if self.lease_ttl <= timedelta(0):
            raise SchedulerLeaseError("lease_ttl must be positive")
        if self.max_retries < 0:
            raise SchedulerLeaseError("max_retries must be non-negative")


@dataclass(frozen=True, slots=True)
class NextRun:
    """The cross-process-readable next-run projection."""

    window: str
    scheduled_for: datetime
    completed: bool
    lease_owner: str | None
    lease_expires_at: datetime | None
    retry_count: int


_MIGRATION = """
CREATE TABLE IF NOT EXISTS schedule_state (
    window          TEXT PRIMARY KEY,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    scheduled_for   TEXT NOT NULL,
    completed       INTEGER NOT NULL DEFAULT 0,
    completed_at    TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT
);
CREATE TABLE IF NOT EXISTS last_completed (
    id              INTEGER PRIMARY KEY CHECK (id = 0),
    window          TEXT NOT NULL,
    completed_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lease (
    window          TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_record (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    window          TEXT NOT NULL,
    owner           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS source_outcome (
    outcome_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    source          TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    detail          TEXT NOT NULL DEFAULT ''
);
"""


def _iso(moment: datetime) -> str:
    return _require_utc(moment, "instant").isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class Scheduler:
    """A once-daily jittered, single-writer, restart-safe worker scheduler.

    Injection points (the linchpin of determinism):

    - ``clock``: ``Callable[[], datetime]`` returning UTC now. All "current
      time" reads go through it; no call reads the real wall clock.
    - ``rng``: ``random.Random`` used solely to pick the jitter offset. A seeded
      instance makes the selected instant reproducible.

    Restart semantics are exercised by discarding a ``Scheduler`` and building a
    fresh one over the same ``database`` path.
    """

    def __init__(
        self,
        database: Path,
        *,
        owner: str,
        config: SchedulerConfig,
        clock: Clock,
        rng: Random,
    ) -> None:
        self._database = Path(database)
        self._owner = owner
        self._config = config
        self._clock = clock
        self._rng = rng
        self._connection = sqlite3.connect(
            self._database, isolation_level=None, timeout=5.0
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_MIGRATION)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Scheduler:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- schedule selection ------------------------------------------------

    def _window_key(self, moment: datetime) -> str:
        return moment.astimezone(UTC).date().isoformat()

    def _nominal_for(self, day: date) -> datetime:
        return datetime.combine(day, self._config.nominal_run_time, tzinfo=UTC)

    def _pick_scheduled(self, nominal: datetime) -> datetime:
        span = int(self._config.jitter.total_seconds())
        if span == 0:
            return nominal
        offset = self._rng.randint(-span, span)
        return nominal + timedelta(seconds=offset)

    def ensure_schedule(self, *, for_date: date | None = None) -> NextRun:
        """Materialize the schedule row for a UTC date, picking the jittered
        instant exactly once. Restart before the target preserves that instant;
        this is why selection is persisted, not recomputed."""

        now = _require_utc(self._clock(), "clock")
        day = for_date or now.date()
        window = day.isoformat()
        row = self._connection.execute(
            "SELECT window FROM schedule_state WHERE window = ?", (window,)
        ).fetchone()
        if row is None:
            nominal = self._nominal_for(day)
            scheduled = self._pick_scheduled(nominal)
            window_start = nominal - self._config.jitter
            window_end = nominal + self._config.jitter
            self._connection.execute(
                "INSERT INTO schedule_state "
                "(window, window_start, window_end, scheduled_for) "
                "VALUES (?, ?, ?, ?)",
                (window, _iso(window_start), _iso(window_end), _iso(scheduled)),
            )
        return self.next_run(window=window)

    # -- lease -------------------------------------------------------------

    def try_acquire(self, *, window: str) -> bool:
        """Attempt to become the single writer for ``window``.

        Returns True only if no live lease exists (or the prior lease expired).
        The write is guarded so exactly one concurrent caller wins."""

        now = _require_utc(self._clock(), "clock")
        expires = now + self._config.lease_ttl
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        try:
            existing = self._connection.execute(
                "SELECT owner, expires_at FROM lease WHERE window = ?", (window,)
            ).fetchone()
            if existing is not None:
                live = _parse(existing[1]) > now
                if live and existing[0] != self._owner:
                    self._connection.execute("ROLLBACK")
                    return False
                self._connection.execute(
                    "UPDATE lease SET owner = ?, acquired_at = ?, expires_at = ? "
                    "WHERE window = ?",
                    (self._owner, _iso(now), _iso(expires), window),
                )
            else:
                self._connection.execute(
                    "INSERT INTO lease (window, owner, acquired_at, expires_at) "
                    "VALUES (?, ?, ?, ?)",
                    (window, self._owner, _iso(now), _iso(expires)),
                )
            self._connection.execute("COMMIT")
            return True
        except sqlite3.OperationalError:
            self._connection.execute("ROLLBACK")
            return False

    def renew(self, *, window: str) -> None:
        """Extend the lease during long-running work so it does not expire mid-run."""

        now = _require_utc(self._clock(), "clock")
        expires = now + self._config.lease_ttl
        updated = self._connection.execute(
            "UPDATE lease SET expires_at = ? WHERE window = ? AND owner = ?",
            (_iso(expires), window, self._owner),
        )
        if updated.rowcount == 0:
            raise SchedulerLeaseError("cannot renew a lease this worker does not hold")

    def release(self, *, window: str) -> None:
        self._connection.execute(
            "DELETE FROM lease WHERE window = ? AND owner = ?", (window, self._owner)
        )

    # -- run + outcome records --------------------------------------------

    def begin_run(self, *, window: str) -> int:
        now = _require_utc(self._clock(), "clock")
        cursor = self._connection.execute(
            "INSERT INTO run_record (window, owner, started_at) VALUES (?, ?, ?)",
            (window, self._owner, _iso(now)),
        )
        return int(cursor.lastrowid or 0)

    def record_source(
        self, *, run_id: int, source: str, ok: bool, detail: str = ""
    ) -> None:
        self._connection.execute(
            "INSERT INTO source_outcome (run_id, source, ok, detail) "
            "VALUES (?, ?, ?, ?)",
            (run_id, source, 1 if ok else 0, detail),
        )

    def finish_run(self, *, run_id: int, window: str, ok: bool) -> None:
        """Close a run. On success mark the window completed and advance
        ``last_completed``; on failure bump retry/backoff *without* creating a
        second daily schedule row."""

        now = _require_utc(self._clock(), "clock")
        self._connection.execute(
            "UPDATE run_record SET finished_at = ?, outcome = ? WHERE run_id = ?",
            (_iso(now), "succeeded" if ok else "failed", run_id),
        )
        if ok:
            self._connection.execute(
                "UPDATE schedule_state SET completed = 1, completed_at = ?, "
                "next_attempt_at = NULL WHERE window = ?",
                (_iso(now), window),
            )
            self._connection.execute(
                "INSERT INTO last_completed (id, window, completed_at) "
                "VALUES (0, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "window = excluded.window, completed_at = excluded.completed_at",
                (window, _iso(now)),
            )
            self.release(window=window)
        else:
            next_attempt = now + self._config.retry_backoff
            self._connection.execute(
                "UPDATE schedule_state SET retry_count = retry_count + 1, "
                "next_attempt_at = ? WHERE window = ?",
                (_iso(next_attempt), window),
            )

    # -- decisions ---------------------------------------------------------

    def should_run(self, *, window: str) -> bool:
        """Restart-safe run gate.

        True only when: the window is not already completed, the current time is
        at/after the selected jittered instant, retry budget is not exhausted,
        any pending retry-backoff has elapsed, and no *other* worker holds a live
        lease."""

        now = _require_utc(self._clock(), "clock")
        row = self._connection.execute(
            "SELECT scheduled_for, completed, retry_count, next_attempt_at "
            "FROM schedule_state WHERE window = ?",
            (window,),
        ).fetchone()
        if row is None:
            return False
        scheduled_for, completed, retry_count, next_attempt_at = row
        if completed:
            return False
        if now < _parse(scheduled_for):
            return False
        if retry_count > self._config.max_retries:
            return False
        if next_attempt_at is not None and now < _parse(next_attempt_at):
            return False
        lease = self._connection.execute(
            "SELECT owner, expires_at FROM lease WHERE window = ?", (window,)
        ).fetchone()
        if lease is not None and lease[0] != self._owner and _parse(lease[1]) > now:
            return False
        return True

    # -- projection (cross-process readable) ------------------------------

    def next_run(self, *, window: str) -> NextRun:
        row = self._connection.execute(
            "SELECT window, scheduled_for, completed, retry_count "
            "FROM schedule_state WHERE window = ?",
            (window,),
        ).fetchone()
        if row is None:
            raise SchedulerLeaseError(f"no schedule for window {window}")
        lease = self._connection.execute(
            "SELECT owner, expires_at FROM lease WHERE window = ?", (window,)
        ).fetchone()
        return NextRun(
            window=row[0],
            scheduled_for=_parse(row[1]),
            completed=bool(row[2]),
            lease_owner=lease[0] if lease else None,
            lease_expires_at=_parse(lease[1]) if lease else None,
            retry_count=int(row[3]),
        )


def read_next_run(database: Path, *, window: str) -> NextRun:
    """Read next-run/progress state over a fresh READ-ONLY connection.

    This simulates the separate read-only API process of the split-service
    pattern: it opens the same SQLite file in read-only mode and never writes.
    """

    uri = f"file:{Path(database)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT window, scheduled_for, completed, retry_count "
            "FROM schedule_state WHERE window = ?",
            (window,),
        ).fetchone()
        if row is None:
            raise SchedulerLeaseError(f"no schedule for window {window}")
        lease = connection.execute(
            "SELECT owner, expires_at FROM lease WHERE window = ?", (window,)
        ).fetchone()
        return NextRun(
            window=row[0],
            scheduled_for=_parse(row[1]),
            completed=bool(row[2]),
            lease_owner=lease[0] if lease else None,
            lease_expires_at=_parse(lease[1]) if lease else None,
            retry_count=int(row[3]),
        )
    finally:
        connection.close()
