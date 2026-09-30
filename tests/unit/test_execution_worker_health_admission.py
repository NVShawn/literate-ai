"""Lifecycle worker admission rechecks bounded health before execution."""

import unittest
from argparse import Namespace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from literate_ai.cli.errors import CliFailure
from literate_ai.cli.execution_workers import (
    SelectedExecutionWorker,
    admit_execution_worker_health,
    dispatch_with_worker_health_poll,
)
from literate_ai.contracts import ExecutionWorker, ExecutionWorkerKind
from literate_ai.contracts.identity import canonical_identity


class ExecutionWorkerHealthAdmissionTests(unittest.TestCase):
    def setUp(self):
        worker = ExecutionWorker(
            "fixture", ExecutionWorkerKind.COMMAND, command=("dispatcher",)
        )
        self.selected = SelectedExecutionWorker(
            worker,
            (),
            canonical_identity({"catalog": "fixture"}),
            Path("/private/workers.json"),
        )
        self.job = canonical_identity({"job": "fixture"})
        self.inputs = SimpleNamespace(policy=SimpleNamespace(maximum_retries=2))

    def loader(self, health, catalog, *, worker_id):
        self.assertEqual(health, "/private/health.json")
        self.assertEqual(catalog, Path("/private/workers.json"))
        self.assertEqual(worker_id, "fixture")
        return self.inputs

    def test_no_configuration_preserves_existing_dispatch(self):
        self.assertIsNone(
            admit_execution_worker_health(
                Namespace(worker_health_config=None), self.selected, self.job
            )
        )

    def test_retry_after_recheck_is_bounded_and_fresh_success_admits(self):
        attempts = []

        def inspector(_inputs, *, job_identity, attempt):
            attempts.append((job_identity, attempt))
            status = 2 if attempt < 2 else 0
            return {"assessment": {"health": "healthy", "decision": "proceed"}}, status

        result = admit_execution_worker_health(
            Namespace(worker_health_config="/private/health.json"),
            self.selected,
            self.job,
            loader=self.loader,
            inspector=inspector,
        )

        self.assertEqual([attempt for _job, attempt in attempts], [0, 1, 2])
        self.assertEqual(result["assessment"]["decision"], "proceed")

    def test_unresolved_deficit_holds_without_unbounded_retry(self):
        attempts = []

        def inspector(_inputs, *, job_identity, attempt):
            attempts.append(attempt)
            return {"assessment": {"health": "critical", "decision": "hold"}}, 1

        with self.assertRaisesRegex(CliFailure, "worker 'fixture' health is critical"):
            admit_execution_worker_health(
                Namespace(worker_health_config="/private/health.json"),
                self.selected,
                self.job,
                loader=self.loader,
                inspector=inspector,
            )

        self.assertEqual(attempts, [0])

    def test_implicit_worker_cannot_misbind_private_health_catalog(self):
        implicit = SelectedExecutionWorker(
            self.selected.worker, (), self.selected.catalog_identity, None
        )
        with self.assertRaisesRegex(CliFailure, "explicitly configured worker"):
            admit_execution_worker_health(
                Namespace(worker_health_config="health.json"), implicit, self.job
            )

    def test_active_poll_reports_hold_without_cancelling_owned_job(self):
        polls = []
        observed_twice = Event()

        def admission(_args, _selected, _job, *, active_job):
            self.assertTrue(active_job)
            polls.append("poll")
            if len(polls) >= 2:
                observed_twice.set()
            return {"assessment": {"health": "critical", "decision": "hold"}}

        def dispatch():
            self.assertTrue(observed_twice.wait(timeout=1.0))
            return "completed"

        result, observations = dispatch_with_worker_health_poll(
            Namespace(
                worker_health_config="/private/health.json",
                worker_health_poll_seconds=0.01,
            ),
            self.selected,
            self.job,
            dispatch,
            admission=admission,
        )

        self.assertEqual(result, "completed")
        self.assertGreaterEqual(len(polls), 2)
        self.assertEqual(len(observations), len(polls))

    def test_dispatch_timeout_error_is_not_mistaken_for_a_poll_interval(self):
        def dispatch():
            raise TimeoutError("dispatcher-owned failure")

        with self.assertRaisesRegex(TimeoutError, "dispatcher-owned failure"):
            dispatch_with_worker_health_poll(
                Namespace(
                    worker_health_config="/private/health.json",
                    worker_health_poll_seconds=0.01,
                ),
                self.selected,
                self.job,
                dispatch,
            )


if __name__ == "__main__":
    unittest.main()
