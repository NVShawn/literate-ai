"""Public storage health must not invent admission, leak paths or mutate state."""

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.worker_health import (
    inspect_worker_storage,
)
from literate_ai.application.worker_pressure import PressureSample
from literate_ai.cli.dispatch import main
from literate_ai.contracts import (
    CapacityMetric,
    CapacityProbeStatus,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    QuotaCapacitySample,
    StorageCapacitySample,
    WorkerCapacityObservation,
)
from tests.support.fixtures_test_worker_capacity import ROLES, policy


class Terminal(io.StringIO):
    def isatty(self):
        return True


class WorkerHealthCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wh-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.family = {"linux": "linux", "darwin": "macos", "win32": "windows"}[
            sys.platform
        ]
        self.worker = ExecutionWorker(
            "fixture",
            ExecutionWorkerKind.LOCAL,
            requirements=ExecutionRequirements(os_family=self.family),
        )
        self.catalog = self.root / "workers.json"
        self.catalog.write_text(
            json.dumps(ExecutionWorkerCatalog((self.worker,)).to_dict())
        )
        for role in ROLES:
            (self.root / role).mkdir()
        capacity = policy(require_quota=False).to_dict()
        capacity.pop("schema")
        capacity.pop("storage_bindings_identity")
        self.config = {
            "schema": "literate-ai/private-worker-health@1",
            "worker_id": "fixture",
            "os_family": self.family,
            "paths": [[role, str(self.root / role)] for role in ROLES],
            "python_executable": None,
            "health_command": None,
            "capacity": capacity,
        }
        self.config_file = self.root / "health.json"
        self.write_config()

    def write_config(self):
        self.config_file.write_text(json.dumps(self.config))

    def invoke(self, *extra, terminal=False):
        output = Terminal() if terminal else io.StringIO()
        errors = io.StringIO()
        status = main(
            [
                "worker",
                "health",
                "--worker-id",
                "fixture",
                "--worker-config",
                str(self.catalog),
                "--health-config",
                str(self.config_file),
                *extra,
            ],
            stdout=output,
            stderr=errors,
        )
        return status, output.getvalue() + errors.getvalue()

    def invoke_cleanup(self, command, *extra):
        output = io.StringIO()
        errors = io.StringIO()
        status = main(
            [
                "worker",
                "cleanup",
                command,
                "--worker-id",
                "fixture",
                "--worker-config",
                str(self.catalog),
                "--health-config",
                str(self.config_file),
                "--json",
                *extra,
            ],
            stdout=output,
            stderr=errors,
        )
        return status, output.getvalue() + errors.getvalue()

    def configure_cleanup(self):
        self.config["cleanup"] = {
            "roots": [
                {
                    "alias": "task-cache",
                    "path": str(self.root / "cache"),
                    "ownership": "task-owned",
                    "recovery": "re-download pinned packages",
                    "active_markers": [".active"],
                    "inactive_markers": [".complete"],
                    "cleanup_command": [
                        sys.executable,
                        "-c",
                        "import shutil,sys;shutil.rmtree(sys.argv[1])",
                        "{target}",
                    ],
                }
            ],
            "deadline_ms": 1000,
            "maximum_entries": 100,
            "maximum_depth": 4,
            "minimum_candidate_bytes": 1,
        }
        self.write_config()

    def observed(
        self,
        bindings,
        selected,
        *,
        job_identity,
        available=1000,
        quota_status=CapacityProbeStatus.NOT_APPLICABLE,
    ):
        now = int(time.time() * 1000)
        return WorkerCapacityObservation(
            bindings.worker.worker_id,
            selected.identity,
            job_identity,
            self.family,
            now - 2,
            now - 1,
            now + selected.maximum_age_ms - 2,
            tuple(
                StorageCapacitySample(
                    role,
                    "volume",
                    2000,
                    CapacityMetric(CapacityProbeStatus.MEASURED, available),
                    CapacityMetric(CapacityProbeStatus.MEASURED, 1000),
                    (
                        QuotaCapacitySample(
                            "quota",
                            CapacityMetric(quota_status),
                            CapacityMetric(CapacityProbeStatus.NOT_APPLICABLE),
                        ),
                    ),
                )
                for role in ROLES
            ),
        )

    def using_observer(self, observer, *, pressure_observer=None):
        def inspect(inputs, **kwargs):
            selected = {"observer": observer, **kwargs}
            if pressure_observer is not None:
                selected["pressure_observer"] = pressure_observer
            return inspect_worker_storage(inputs, **selected)

        return patch(
            "literate_ai.cli.worker_health.inspect_worker_storage", side_effect=inspect
        )

    def test_sustained_memory_pressure_holds_lifecycle_admission(self):
        self.config["pressure"] = {
            "sustained_samples": 3,
            "sample_interval_ms": 0,
            "cpu_warning_basis_points": 8000,
            "minimum_available_memory_bytes": 1024,
            "maximum_paging_bytes_per_second": 100,
            "minimum_gpu_free_memory_bytes": 512,
            "require_cpu": True,
            "require_memory": True,
            "require_gpu": False,
        }
        self.write_config()

        def pressure_observer(_bindings, _policy):
            return tuple(
                PressureSample(
                    index,
                    CapacityMetric(CapacityProbeStatus.MEASURED, 9000),
                    CapacityMetric(CapacityProbeStatus.MEASURED, 512),
                    CapacityMetric(CapacityProbeStatus.MEASURED, 101),
                    CapacityMetric(CapacityProbeStatus.MEASURED, 7),
                    CapacityMetric(CapacityProbeStatus.MEASURED, index),
                )
                for index in range(3)
            )

        with self.using_observer(self.observed, pressure_observer=pressure_observer):
            status, output = self.invoke("--json")
            human_status, human = self.invoke(terminal=True)

        self.assertEqual((status, human_status), (1, 1))
        result = json.loads(output)["result"]
        self.assertEqual(result["assessment"]["decision"], "proceed")
        self.assertIn(
            "sustained-memory-pressure",
            {item["reason"] for item in result["pressure"]["findings"]},
        )
        self.assertEqual(result["pressure"]["status"], "observed")
        self.assertIn("worker: memory critical", human)
        self.assertIn("measured=512 threshold=1024", human)

    def test_cleanup_plan_does_not_delete_and_apply_requires_exact_authorization(self):
        self.configure_cleanup()
        candidate = self.root / "cache" / "completed-task"
        candidate.mkdir()
        (candidate / ".complete").write_text("done")
        (candidate / "payload").write_bytes(b"x" * 32)

        status, output = self.invoke_cleanup("plan")

        self.assertEqual(status, 0, output)
        proposal = json.loads(output)["result"]
        self.assertTrue(candidate.is_dir())
        self.assertFalse(proposal["deletion_authorized"])
        self.assertEqual(len(proposal["targets"]), 1)
        authorization = {
            "schema": "literate-ai/worker-cleanup-authorization@1",
            "worker_id": proposal["worker_id"],
            "policy_identity": proposal["policy_identity"],
            "proposal_identity": proposal["proposal_identity"],
            "proposal_created_at_ms": proposal["proposal_created_at_ms"],
            "target_ids": [proposal["targets"][0]["candidate_id"]],
            "operation": proposal["operation"],
            "expires_at_ms": int(time.time() * 1000) + 60000,
        }
        authorization_path = self.root / "cleanup-authorization.json"
        authorization_path.write_text(json.dumps(authorization))

        status, output = self.invoke_cleanup(
            "apply", "--authorization", str(authorization_path)
        )

        self.assertEqual(status, 0, output)
        result = json.loads(output)["result"]
        self.assertFalse(candidate.exists())
        self.assertEqual(result["proposal_identity"], proposal["proposal_identity"])
        self.assertEqual(len(result["receipts"]), 1)

    def test_real_local_measurement_preserves_files_and_suppresses_implicit_writes(
        self,
    ):
        marker = self.root / "temp" / "keep"
        marker.write_text("owned active content")
        before = {
            p.relative_to(self.root): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        with (
            patch(
                "literate_ai.cli.dispatch.maybe_host_self_update",
                side_effect=AssertionError("update"),
            ),
            patch(
                "literate_ai.cli.dispatch.ensure_user_mcp_catalog",
                side_effect=AssertionError("catalog"),
            ),
            patch(
                "literate_ai.cli.dispatch._run_operator_mcp_discovery",
                side_effect=AssertionError("discovery"),
            ),
            patch(
                "literate_ai.cli.dispatch.journal_mutagenic_event",
                side_effect=AssertionError("journal"),
            ),
            patch(
                "literate_ai.cli.dispatch.PerformanceRecorder.span",
                side_effect=AssertionError("telemetry"),
            ),
        ):
            status, output = self.invoke("--json")
        self.assertEqual(status, 0, output)
        result = json.loads(output)["result"]
        self.assertEqual(result["scope"], "storage")
        self.assertEqual(result["assessment"]["decision"], "proceed")
        self.assertTrue(
            all(
                s["available_bytes"]["status"] == "measured"
                for s in result["observation"]["samples"]
            )
        )
        self.assertNotIn(str(self.root), output)
        self.assertEqual(
            before,
            {
                p.relative_to(self.root): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )


if __name__ == "__main__":
    unittest.main()
