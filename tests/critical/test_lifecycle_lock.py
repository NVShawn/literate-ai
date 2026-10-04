"""Regression coverage for the project-scoped lifecycle mutation lock.

Issue #318: two concurrent Standard rebuilds (or a rebuild racing
``--update-receipt``) can each pass planning and then race the same
project's shared checkpoint/finalized-receipt publication state, so one
later fails an internal predecessor-contract check after already paying for
expensive model-backed generation. `project_lifecycle_lock` gives every
project-scoped mutating lifecycle operation one advisory lock so a second
concurrent mutation is rejected -- with a typed diagnostic naming the
current holder -- before it starts. These tests use a
`threading.Barrier` to force a deterministic interleaving (the holder is
guaranteed to be inside the lock when the contender attempts to acquire it)
instead of a flaky sleep-based race.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
    project_lifecycle_lock_path,
)


def _hold_and_die(root: str) -> None:
    """Module-level so ``multiprocessing``'s ``spawn`` context can pickle it.

    Acquires the lock and never releases it: simulates a hard crash while
    the lock is held, then exits the process immediately.
    """

    cm = project_lifecycle_lock(Path(root), operation="rebuild")
    cm.__enter__()
    os._exit(1)


class ProjectLifecycleLockConcurrencyTests(unittest.TestCase):
    def test_second_concurrent_mutation_fails_closed_with_owner_diagnostic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            holder_barrier = threading.Barrier(2)
            release_event = threading.Event()
            holder_error: list[BaseException] = []
            contender_error: list[BaseException | None] = [None]

            def hold() -> None:
                try:
                    with project_lifecycle_lock(project_root, operation="rebuild"):
                        # Signal the contender only once the OS-level lock and
                        # owner diagnostics are published, then block until the
                        # contender has made (and failed) its own attempt.
                        holder_barrier.wait(timeout=5)
                        release_event.wait(timeout=5)
                except BaseException as exc:  # noqa: BLE001 - captured for assertion
                    holder_error.append(exc)

            holder_thread = threading.Thread(target=hold)
            holder_thread.start()
            holder_barrier.wait(timeout=5)
            try:
                try:
                    with project_lifecycle_lock(
                        project_root, operation="update-receipt"
                    ):
                        pass
                except ProjectLifecycleLockError as exc:
                    contender_error[0] = exc
            finally:
                release_event.set()
                holder_thread.join(timeout=5)

            self.assertEqual(holder_error, [])
            self.assertIsNotNone(contender_error[0])
            exc = contender_error[0]
            assert exc is not None
            self.assertEqual(exc.code, "lifecycle.project_locked")
            self.assertIn("rebuild", exc.message)
            self.assertIn(str(os.getpid()), exc.message)

    def test_crashed_holder_process_does_not_leave_a_permanent_stale_lock(
        self,
    ) -> None:
        """A holder that dies without releasing must not wedge the project.

        `project_lifecycle_lock` is built on the same OS-level
        `fcntl.flock`/`msvcrt.locking` primitive already used for immutable
        local-cache publication (`literate_ai._cache_lock`): the operating
        system releases that advisory lock the instant the holding process
        exits for any reason, crash included, with no PID-file or heartbeat
        bookkeeping required.
        """

        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            lock_path = project_lifecycle_lock_path(project_root)
            self.assertTrue(
                str(lock_path).startswith(str(project_root.resolve(strict=True)))
            )

            import multiprocessing

            process = multiprocessing.get_context("spawn").Process(
                target=_hold_and_die, args=(str(project_root),)
            )
            process.start()
            process.join(timeout=15)
            self.assertFalse(process.is_alive())

            with project_lifecycle_lock(project_root, operation="rebuild"):
                pass


if __name__ == "__main__":
    unittest.main()
