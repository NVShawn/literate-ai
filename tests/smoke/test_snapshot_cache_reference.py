"""Deterministic acceptance tests for the coherent-snapshot cache contract.

Each test maps to an acceptance property of issue #216 (SPLIT-SERVICE-001) as
decided by ADR 0027 and specified by the
``specification-to-source/backend-application/durable-split-service`` skill. The
tests exercise the reference implementation in
``tests.support.snapshot_cache_reference`` with a fixed UTC clock; nothing reads
a real wall clock, so results are reproducible under xdist and pytest-randomly.

Coverage note (offline vs. deferred live sample):

- PROVEN OFFLINE here: coherent current-snapshot publication (readers see the
  last complete snapshot); a failed/partial write leaves readers on the prior
  complete snapshot; readers advance only after a complete publish; reader
  connections fail closed on write; the frontend->API->cache read path never
  reaches upstream; restarting the reader/API serves the persisted snapshot
  without invoking a collector.
- REQUIRES the deferred live portfolio sample: "the collector is the ONLY
  Component whose generated code can invoke an upstream fixture" and "a portfolio
  sample composes four Components through public capabilities" (plus Component
  locks / CycloneDX evidence and the macOS/Linux/Windows sample matrix). Those
  are only PARTIALLY covered offline: this suite models the API as having no
  upstream client (``ForbiddenUpstream``) and asserts the read path never calls
  one, but the full "only the collector may call upstream" claim across four
  generated Components can only be closed by the live sample.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.support.snapshot_cache_reference import (
    ApiReadModel,
    ForbiddenUpstream,
    Freshness,
    Snapshot,
    SnapshotCacheError,
    SnapshotReader,
    SnapshotWriter,
    migrate,
)

_T0 = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)


class SnapshotCacheContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="litai-snapshot-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "cache.sqlite3"
        migrate(self.db)

    def _writer(self) -> SnapshotWriter:
        writer = SnapshotWriter(self.db)
        self.addCleanup(writer.close)
        return writer

    def _publish_window(
        self,
        writer: SnapshotWriter,
        *,
        window: str,
        rows: dict[str, float],
        now: datetime,
    ) -> int:
        snapshot_id = writer.begin_snapshot(window=window, now=now)
        for metric, value in rows.items():
            writer.write_row(snapshot_id=snapshot_id, metric=metric, value=value)
        writer.publish(snapshot_id=snapshot_id, required_metrics=tuple(rows), now=now)
        return snapshot_id

    # -- Acceptance: coherent current-snapshot publication -----------------

    def test_no_snapshot_before_first_publish(self) -> None:
        with SnapshotReader(self.db) as reader:
            self.assertIsNone(reader.current_snapshot())

    def test_after_complete_publish_readers_see_new_snapshot(self) -> None:
        writer = self._writer()
        self._publish_window(
            writer, window="2026-09-01", rows={"cpu": 1.0, "mem": 2.0}, now=_T0
        )
        with SnapshotReader(self.db) as reader:
            snapshot = reader.current_snapshot()
        assert snapshot is not None
        self.assertIsInstance(snapshot, Snapshot)
        self.assertEqual(snapshot.window, "2026-09-01")
        self.assertEqual(snapshot.rows, (("cpu", 1.0), ("mem", 2.0)))

    # -- Acceptance: reader during in-progress collection sees PRIOR snapshot

    def test_in_progress_collection_keeps_readers_on_prior_snapshot(self) -> None:
        writer = self._writer()
        first_id = self._publish_window(
            writer, window="2026-09-01", rows={"cpu": 1.0}, now=_T0
        )
        # A second collection begins and writes PARTIAL rows but does not publish.
        in_progress = writer.begin_snapshot(
            window="2026-09-02", now=_T0 + timedelta(days=1)
        )
        writer.write_row(snapshot_id=in_progress, metric="cpu", value=9.0)
        # Reader still sees the FIRST complete snapshot, not the half-written one.
        with SnapshotReader(self.db) as reader:
            snapshot = reader.current_snapshot()
            freshness = reader.freshness()
        assert snapshot is not None
        self.assertEqual(snapshot.snapshot_id, first_id)
        self.assertEqual(snapshot.window, "2026-09-01")
        self.assertEqual(snapshot.rows, (("cpu", 1.0),))
        # Freshness metadata still names the in-progress window as collecting.
        self.assertEqual(freshness.current_window, "2026-09-01")
        self.assertEqual(freshness.collecting_window, "2026-09-02")

    # -- Acceptance: failed/aborted partial write -> prior complete snapshot

    def test_aborted_partial_write_leaves_prior_snapshot(self) -> None:
        writer = self._writer()
        first_id = self._publish_window(
            writer, window="2026-09-01", rows={"cpu": 1.0}, now=_T0
        )
        partial = writer.begin_snapshot(
            window="2026-09-02", now=_T0 + timedelta(days=1)
        )
        writer.write_row(snapshot_id=partial, metric="cpu", value=9.0)
        writer.abort(snapshot_id=partial)
        with SnapshotReader(self.db) as reader:
            snapshot = reader.current_snapshot()
        assert snapshot is not None
        self.assertEqual(snapshot.snapshot_id, first_id)
        self.assertEqual(snapshot.rows, (("cpu", 1.0),))

    def test_publish_missing_required_write_fails_and_keeps_prior(self) -> None:
        writer = self._writer()
        first_id = self._publish_window(
            writer, window="2026-09-01", rows={"cpu": 1.0, "mem": 2.0}, now=_T0
        )
        # Next window writes only ONE of two required metrics, then tries publish.
        incomplete = writer.begin_snapshot(
            window="2026-09-02", now=_T0 + timedelta(days=1)
        )
        writer.write_row(snapshot_id=incomplete, metric="cpu", value=5.0)
        with self.assertRaises(SnapshotCacheError):
            writer.publish(
                snapshot_id=incomplete,
                required_metrics=("cpu", "mem"),
                now=_T0 + timedelta(days=1),
            )
        # The failed publish did not advance the pointer.
        with SnapshotReader(self.db) as reader:
            snapshot = reader.current_snapshot()
        assert snapshot is not None
        self.assertEqual(snapshot.snapshot_id, first_id)
        self.assertEqual(snapshot.window, "2026-09-01")

    def test_pointer_advances_only_after_full_second_publish(self) -> None:
        writer = self._writer()
        self._publish_window(writer, window="2026-09-01", rows={"cpu": 1.0}, now=_T0)
        second_id = self._publish_window(
            writer,
            window="2026-09-02",
            rows={"cpu": 3.0},
            now=_T0 + timedelta(days=1),
        )
        with SnapshotReader(self.db) as reader:
            snapshot = reader.current_snapshot()
        assert snapshot is not None
        self.assertEqual(snapshot.snapshot_id, second_id)
        self.assertEqual(snapshot.window, "2026-09-02")
        self.assertEqual(snapshot.rows, (("cpu", 3.0),))

    # -- Acceptance: read-only reader connection cannot write (fail closed)

    def test_reader_connection_cannot_write(self) -> None:
        self._publish_window(
            self._writer(), window="2026-09-01", rows={"cpu": 1.0}, now=_T0
        )
        with SnapshotReader(self.db) as reader:
            with self.assertRaises(sqlite3.OperationalError):
                reader.try_write()

    def test_api_read_model_has_no_upstream_client(self) -> None:
        api = ApiReadModel(self.db)
        self.assertIsInstance(api.upstream, ForbiddenUpstream)
        with self.assertRaises(SnapshotCacheError):
            _ = api.upstream.fetch  # any upstream access fails closed

    # -- Acceptance: four-boundary READ path never reaches upstream --------

    def test_read_path_frontend_api_cache_never_reaches_upstream(self) -> None:
        writer = self._writer()
        self._publish_window(
            writer, window="2026-09-01", rows={"cpu": 1.0, "mem": 2.0}, now=_T0
        )
        api = ApiReadModel(self.db)
        # The "frontend" consumes ONLY the API's read result.
        snapshot = api.read_current()
        freshness = api.read_freshness()
        assert snapshot is not None
        self.assertEqual(snapshot.window, "2026-09-01")
        self.assertIsInstance(freshness, Freshness)
        self.assertEqual(freshness.current_window, "2026-09-01")
        # Any upstream access from the read path would have raised; it did not.

    # -- Acceptance: restart API/frontend serves persisted snapshot --------

    def test_restart_reader_serves_persisted_snapshot_without_collector(self) -> None:
        writer = self._writer()
        self._publish_window(writer, window="2026-09-01", rows={"cpu": 1.0}, now=_T0)
        writer.close()
        # "Restart" the API: a brand-new read model over the same DB file, with
        # NO writer/collector alive. It still serves the persisted snapshot.
        api = ApiReadModel(self.db)
        snapshot = api.read_current()
        assert snapshot is not None
        self.assertEqual(snapshot.window, "2026-09-01")
        self.assertEqual(snapshot.rows, (("cpu", 1.0),))


if __name__ == "__main__":
    unittest.main()
