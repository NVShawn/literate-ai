"""Deterministic acceptance tests for the scheduler-lease behavioral contract.

Each test maps to one acceptance bullet of issue #217 (SCHEDULER-LEASE-001) and
exercises the reference implementation in
``tests.support.scheduler_lease_reference`` with an injected clock and seeded
random. Nothing here reads a real wall clock or unseeded randomness, so results
are reproducible under xdist and pytest-randomly.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from random import Random

from tests.support.scheduler_lease_reference import (
    NextRun,
    Scheduler,
    SchedulerConfig,
    read_next_run,
)


class _Clock:
    """A mutable injected clock returning a fixed UTC instant until advanced."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


def _config(**overrides: object) -> SchedulerConfig:
    base: dict[str, object] = {
        "nominal_run_time": time(3, 0, 0),
        "jitter": timedelta(minutes=30),
        "lease_ttl": timedelta(minutes=15),
        "max_retries": 3,
        "retry_backoff": timedelta(minutes=5),
    }
    base.update(overrides)
    return SchedulerConfig(**base)  # type: ignore[arg-type]


class SchedulerLeaseAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="litai-scheduler-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "cache.sqlite3"

    def _scheduler(
        self, *, owner: str, clock: _Clock, seed: int, config: SchedulerConfig
    ) -> Scheduler:
        scheduler = Scheduler(
            self.db,
            owner=owner,
            config=config,
            clock=clock,
            rng=Random(seed),
        )
        self.addCleanup(scheduler.close)
        return scheduler

    # 1. negative AND positive jitter bounds around nominal time
    def test_jitter_stays_within_symmetric_bounds_both_signs(self) -> None:
        nominal = time(3, 0, 0)
        jitter = timedelta(minutes=30)
        config = _config(nominal_run_time=nominal, jitter=jitter)
        day = date(2026, 1, 15)
        nominal_instant = datetime.combine(day, nominal, tzinfo=UTC)
        clock = _Clock(datetime(2026, 1, 15, 0, 0, tzinfo=UTC))
        offsets: set[float] = set()
        for seed in range(200):
            with tempfile.TemporaryDirectory() as tmp:
                scheduler = Scheduler(
                    Path(tmp) / "c.sqlite3",
                    owner="w",
                    config=config,
                    clock=clock,
                    rng=Random(seed),
                )
                try:
                    run = scheduler.ensure_schedule(for_date=day)
                finally:
                    scheduler.close()
            delta = run.scheduled_for - nominal_instant
            offsets.add(delta.total_seconds())
            self.assertLessEqual(abs(delta), jitter)
        # Both a strictly-negative and a strictly-positive offset are reachable.
        self.assertTrue(any(value < 0 for value in offsets))
        self.assertTrue(any(value > 0 for value in offsets))

    # 2. UTC date rollover and clock movement
    def test_utc_date_rollover_creates_a_distinct_next_window(self) -> None:
        config = _config()
        clock = _Clock(datetime(2026, 3, 10, 2, 0, tzinfo=UTC))
        scheduler = self._scheduler(owner="w", clock=clock, seed=1, config=config)
        first = scheduler.ensure_schedule()
        self.assertEqual(first.window, "2026-03-10")
        clock.set(datetime(2026, 3, 11, 2, 0, tzinfo=UTC))
        second = scheduler.ensure_schedule()
        self.assertEqual(second.window, "2026-03-11")
        self.assertNotEqual(first.window, second.window)
        self.assertEqual(first.scheduled_for.date(), date(2026, 3, 10))
        self.assertEqual(second.scheduled_for.date(), date(2026, 3, 11))

    # 3a. restart BEFORE target preserves the selected instant
    def test_restart_before_target_preserves_selected_instant(self) -> None:
        config = _config()
        clock = _Clock(datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
        first = self._scheduler(owner="w", clock=clock, seed=7, config=config)
        run = first.ensure_schedule()
        first.close()
        # "Restart": a brand-new scheduler over the same DB with different RNG.
        second = Scheduler(
            self.db, owner="w", config=config, clock=clock, rng=Random(999)
        )
        self.addCleanup(second.close)
        again = second.ensure_schedule()
        self.assertEqual(again.scheduled_for, run.scheduled_for)
        self.assertFalse(second.should_run(window=again.window))

    # 3b. restart AFTER success does not rerun
    def test_restart_after_success_does_not_rerun(self) -> None:
        config = _config()
        clock = _Clock(datetime(2026, 4, 2, 3, 0, tzinfo=UTC))
        first = self._scheduler(owner="w", clock=clock, seed=2, config=config)
        run = first.ensure_schedule()
        clock.set(run.scheduled_for + timedelta(minutes=1))
        self.assertTrue(first.should_run(window=run.window))
        self.assertTrue(first.try_acquire(window=run.window))
        run_id = first.begin_run(window=run.window)
        first.record_source(run_id=run_id, source="alpha", ok=True)
        first.finish_run(run_id=run_id, window=run.window, ok=True)
        first.close()
        # Restart after completion: same window must not run again.
        second = Scheduler(
            self.db, owner="w", config=config, clock=clock, rng=Random(3)
        )
        self.addCleanup(second.close)
        second.ensure_schedule()
        self.assertFalse(second.should_run(window=run.window))
        self.assertTrue(second.next_run(window=run.window).completed)

    # 3c. restart AFTER failure (retry allowed, still one window)
    def test_restart_after_failure_allows_retry_same_window(self) -> None:
        config = _config(retry_backoff=timedelta(minutes=5))
        clock = _Clock(datetime(2026, 4, 3, 3, 0, tzinfo=UTC))
        first = self._scheduler(owner="w", clock=clock, seed=5, config=config)
        run = first.ensure_schedule()
        clock.set(run.scheduled_for + timedelta(minutes=1))
        self.assertTrue(first.try_acquire(window=run.window))
        run_id = first.begin_run(window=run.window)
        first.record_source(run_id=run_id, source="alpha", ok=False, detail="boom")
        first.finish_run(run_id=run_id, window=run.window, ok=False)
        first.close()
        second = Scheduler(
            self.db, owner="w", config=config, clock=clock, rng=Random(6)
        )
        self.addCleanup(second.close)
        second.ensure_schedule()
        # Backoff not elapsed yet: gated.
        self.assertFalse(second.should_run(window=run.window))
        clock.advance(timedelta(minutes=6))
        self.assertTrue(second.should_run(window=run.window))
        self.assertEqual(second.next_run(window=run.window).retry_count, 1)

    # 4. concurrent scheduler instances with exactly one winner
    def test_concurrent_instances_exactly_one_winner(self) -> None:
        config = _config()
        clock = _Clock(datetime(2026, 5, 1, 3, 0, tzinfo=UTC))
        primary = self._scheduler(owner="alpha", clock=clock, seed=1, config=config)
        run = primary.ensure_schedule()
        clock.set(run.scheduled_for + timedelta(minutes=1))
        secondary = self._scheduler(owner="beta", clock=clock, seed=1, config=config)
        results = [
            primary.try_acquire(window=run.window),
            secondary.try_acquire(window=run.window),
        ]
        self.assertEqual(sum(1 for value in results if value), 1)
        winner = "alpha" if results[0] else "beta"
        self.assertEqual(primary.next_run(window=run.window).lease_owner, winner)

    # 5. expired lease recovery
    def test_expired_lease_is_recoverable_by_another_worker(self) -> None:
        config = _config(lease_ttl=timedelta(minutes=15))
        clock = _Clock(datetime(2026, 6, 1, 3, 0, tzinfo=UTC))
        dead = self._scheduler(owner="dead", clock=clock, seed=1, config=config)
        run = dead.ensure_schedule()
        clock.set(run.scheduled_for + timedelta(minutes=1))
        self.assertTrue(dead.try_acquire(window=run.window))
        rescuer = self._scheduler(owner="rescuer", clock=clock, seed=1, config=config)
        # Before expiry the rescuer cannot steal the live lease.
        self.assertFalse(rescuer.try_acquire(window=run.window))
        self.assertFalse(rescuer.should_run(window=run.window))
        # After TTL the stale lease is recoverable.
        clock.advance(timedelta(minutes=16))
        self.assertTrue(rescuer.should_run(window=run.window))
        self.assertTrue(rescuer.try_acquire(window=run.window))
        self.assertEqual(rescuer.next_run(window=run.window).lease_owner, "rescuer")

    # 6. long-running work crossing the next nominal boundary
    def test_long_run_crossing_next_boundary_does_not_double_run(self) -> None:
        config = _config(lease_ttl=timedelta(hours=6))
        clock = _Clock(datetime(2026, 7, 1, 3, 0, tzinfo=UTC))
        worker = self._scheduler(owner="w", clock=clock, seed=1, config=config)
        today = worker.ensure_schedule()
        clock.set(today.scheduled_for + timedelta(minutes=1))
        self.assertTrue(worker.try_acquire(window=today.window))
        run_id = worker.begin_run(window=today.window)
        # Work runs long, crossing into the next UTC day; renew keeps the lease.
        clock.advance(timedelta(hours=25))
        worker.renew(window=today.window)
        next_day = worker.ensure_schedule()
        self.assertNotEqual(next_day.window, today.window)
        # Yesterday's window is still owned and still not completed; finishing it
        # completes exactly that window, not the new one.
        worker.finish_run(run_id=run_id, window=today.window, ok=True)
        self.assertTrue(worker.next_run(window=today.window).completed)
        self.assertFalse(worker.next_run(window=next_day.window).completed)
        # The long run did not double-run the ORIGINAL window: exactly one run
        # record exists for it even though the clock crossed a nominal boundary.
        original_runs = worker._connection.execute(  # noqa: SLF001 - reference assert
            "SELECT COUNT(*) FROM run_record WHERE window = ?", (today.window,)
        ).fetchone()
        self.assertEqual(original_runs[0], 1)
        # The next day is a distinct, still-open window with its own schedule row,
        # not a rerun of the completed one.
        self.assertFalse(worker.next_run(window=next_day.window).completed)

    # 7. retry behavior does not create a second daily run
    def test_retries_do_not_create_a_second_daily_run(self) -> None:
        config = _config(max_retries=3, retry_backoff=timedelta(minutes=1))
        clock = _Clock(datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
        worker = self._scheduler(owner="w", clock=clock, seed=1, config=config)
        run = worker.ensure_schedule()
        clock.set(run.scheduled_for + timedelta(minutes=1))
        for _attempt in range(3):
            self.assertTrue(worker.should_run(window=run.window))
            self.assertTrue(worker.try_acquire(window=run.window))
            run_id = worker.begin_run(window=run.window)
            worker.record_source(run_id=run_id, source="alpha", ok=False)
            worker.finish_run(run_id=run_id, window=run.window, ok=False)
            clock.advance(timedelta(minutes=2))
        # Exactly one schedule row exists for the day regardless of retries.
        rows = worker._connection.execute(  # noqa: SLF001 - reference-only assertion
            "SELECT COUNT(*) FROM schedule_state WHERE window = ?", (run.window,)
        ).fetchone()
        self.assertEqual(rows[0], 1)
        self.assertEqual(worker.next_run(window=run.window).retry_count, 3)
        # A successful attempt then completes the same single window.
        self.assertTrue(worker.try_acquire(window=run.window))
        run_id = worker.begin_run(window=run.window)
        worker.record_source(run_id=run_id, source="alpha", ok=True)
        worker.finish_run(run_id=run_id, window=run.window, ok=True)
        self.assertTrue(worker.next_run(window=run.window).completed)

    def test_retry_budget_exhaustion_stops_the_window(self) -> None:
        config = _config(max_retries=1, retry_backoff=timedelta(minutes=1))
        clock = _Clock(datetime(2026, 8, 2, 3, 0, tzinfo=UTC))
        worker = self._scheduler(owner="w", clock=clock, seed=1, config=config)
        run = worker.ensure_schedule()
        clock.set(run.scheduled_for + timedelta(minutes=1))
        for _ in range(2):
            self.assertTrue(worker.try_acquire(window=run.window))
            run_id = worker.begin_run(window=run.window)
            worker.finish_run(run_id=run_id, window=run.window, ok=False)
            clock.advance(timedelta(minutes=2))
        # retry_count now exceeds max_retries; the window is abandoned.
        self.assertFalse(worker.should_run(window=run.window))

    # 8. next-run/progress state readable by a separate process
    def test_next_run_state_readable_from_separate_readonly_connection(self) -> None:
        config = _config()
        clock = _Clock(datetime(2026, 9, 1, 3, 0, tzinfo=UTC))
        worker = self._scheduler(owner="api-peer", clock=clock, seed=42, config=config)
        run = worker.ensure_schedule()
        # A second, independent read-only connection (the API process) sees it.
        observed = read_next_run(self.db, window=run.window)
        self.assertIsInstance(observed, NextRun)
        self.assertEqual(observed.scheduled_for, run.scheduled_for)
        self.assertFalse(observed.completed)
        # Progress made by the writer is visible to the reader without a handoff.
        clock.set(run.scheduled_for + timedelta(minutes=1))
        self.assertTrue(worker.try_acquire(window=run.window))
        during = read_next_run(self.db, window=run.window)
        self.assertEqual(during.lease_owner, "api-peer")
        self.assertIsNotNone(during.lease_expires_at)
        run_id = worker.begin_run(window=run.window)
        worker.finish_run(run_id=run_id, window=run.window, ok=True)
        after = read_next_run(self.db, window=run.window)
        self.assertTrue(after.completed)


if __name__ == "__main__":
    unittest.main()
