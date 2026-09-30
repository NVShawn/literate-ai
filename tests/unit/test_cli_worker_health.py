"""Public storage health must not invent admission, leak paths or mutate state."""

import io
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.worker_health import (
    inspect_worker_storage,
    load_worker_health_inputs,
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
    canonical_identity,
)
from tests.unit.test_worker_capacity import ROLES, policy


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

    def test_remote_windows_cleanup_paths_are_not_resolved_on_controller(self):
        worker = ExecutionWorker(
            "windows-worker",
            ExecutionWorkerKind.COMMAND,
            requirements=ExecutionRequirements(os_family="windows"),
            command=("dispatcher",),
        )
        catalog = self.root / "windows-workers.json"
        catalog.write_text(json.dumps(ExecutionWorkerCatalog((worker,)).to_dict()))
        config = {
            **self.config,
            "worker_id": worker.worker_id,
            "os_family": "windows",
            "paths": [[role, rf"C:\worker\{role}"] for role in ROLES],
            "cleanup": {
                "roots": [
                    {
                        "alias": "task-cache",
                        "path": r"C:\worker\cache\tasks",
                        "ownership": "task-owned",
                        "recovery": "re-download pinned packages",
                        "active_markers": [".active"],
                        "inactive_markers": [".complete"],
                        "cleanup_command": ["cleanup-tool.exe", "{target}"],
                    }
                ],
                "deadline_ms": 1000,
                "maximum_entries": 100,
                "maximum_depth": 4,
                "minimum_candidate_bytes": 1,
            },
        }
        health = self.root / "windows-health.json"
        health.write_text(json.dumps(config))

        inputs = load_worker_health_inputs(health, catalog, worker_id=worker.worker_id)

        self.assertEqual(inputs.cleanup.roots[0].path, r"C:\worker\cache\tasks")

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

    def test_help_names_private_inputs_and_no_cleanup_option(self):
        output = io.StringIO()
        self.assertEqual(main(["worker", "health", "help"], stdout=output), 0)
        self.assertIn("--health-config", output.getvalue())
        self.assertNotIn("--authorize-delete", output.getvalue())

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

    def test_held_capacity_is_visible_in_json_and_human_alerts(self):
        for role in self.config["capacity"]["roles"]:
            role["additional_bytes"] = 20
            role["reserve_bytes"] = 10
        self.write_config()
        with self.using_observer(
            lambda *a, **kw: self.observed(*a, **kw, available=89)
        ):
            status, output = self.invoke("--json")
            human_status, human = self.invoke(terminal=True)
        self.assertEqual((status, human_status), (1, 1))
        result = json.loads(output)["result"]
        alert = next(a for a in result["alerts"] if a["resource"] == "bytes")
        self.assertEqual(alert["deficit"], 1)
        self.assertEqual(alert["impact"], "hold")
        self.assertIn("deficit=1", human)
        self.assertIn("exact-target authorization", human)
        self.assertNotIn(str(self.root), human)
        self.assertEqual(result["cleanup_investigation"]["status"], "not-configured")

    def test_disk_incident_automatically_investigates_configured_task_cache(self):
        candidate = self.root / "cache" / "old-wheelhouse"
        candidate.mkdir()
        (candidate / "wheel").write_bytes(b"x" * 32)
        self.config["cleanup"] = {
            "roots": [
                {
                    "alias": "package-cache",
                    "path": str(self.root / "cache"),
                    "ownership": "task-owned",
                    "recovery": "re-download pinned packages",
                    "active_markers": [".active"],
                    "inactive_markers": [".complete"],
                    "cleanup_command": ["cleanup-tool", "{target}"],
                }
            ],
            "deadline_ms": 1000,
            "maximum_entries": 100,
            "maximum_depth": 4,
            "minimum_candidate_bytes": 1,
        }
        for role in self.config["capacity"]["roles"]:
            role["reserve_bytes"] = 100
        self.write_config()

        with self.using_observer(
            lambda *a, **kw: self.observed(*a, **kw, available=99)
        ):
            status, output = self.invoke("--json")

        self.assertEqual(status, 1)
        result = json.loads(output)["result"]
        investigation = result["cleanup_investigation"]
        self.assertEqual(investigation["status"], "complete")
        self.assertEqual(len(investigation["candidates"]), 1)
        self.assertEqual(investigation["candidates"][0]["root"], "package-cache")
        self.assertEqual(investigation["candidates"][0]["active_use"], "uncertain")
        self.assertFalse(investigation["deletion_authorized"])
        self.assertNotIn(str(self.root), output)

    def test_required_unknown_returns_retry_and_optional_unknown_stays_visible(self):
        def observer(*args, **kwargs):
            return self.observed(
                *args, **kwargs, quota_status=CapacityProbeStatus.DENIED
            )

        for required, expected in ((False, 0), (True, 2)):
            for role in self.config["capacity"]["roles"]:
                role["require_quota"] = required
            self.write_config()
            with self.using_observer(observer):
                status, output = self.invoke("--json")
            self.assertEqual(status, expected)
            self.assertEqual(
                json.loads(output)["result"]["assessment"]["health"], "unknown"
            )
            self.assertIn("probe-denied", output)

    def test_job_identity_is_bound_and_path_changes_change_effective_policy(self):
        job = canonical_identity("job").uri
        with self.using_observer(self.observed):
            _, first = self.invoke("--job-identity", job, "--json")
            self.config["paths"][0][1] = str(self.root / "temp")
            self.write_config()
            _, second = self.invoke("--job-identity", job, "--json")
        first, second = (json.loads(s)["result"] for s in (first, second))
        self.assertEqual(first["assessment"]["job_identity"], job)
        self.assertNotEqual(
            first["assessment"]["policy_identity"],
            second["assessment"]["policy_identity"],
        )

    def test_changed_configuration_during_measurement_refuses_without_leaking_content(
        self,
    ):
        def observer(*args, **kwargs):
            observed = self.observed(*args, **kwargs)
            self.config_file.write_text("private-secret-invalid")
            return observed

        with self.using_observer(observer):
            status, output = self.invoke("--json")
        self.assertNotEqual(status, 0)
        self.assertIn("worker.health_input_invalid", output)
        self.assertNotIn("private-secret-invalid", output)
        self.assertNotIn(str(self.root), output)

    def test_ambiguous_unknown_oversized_and_wrong_worker_inputs_refuse_before_probe(
        self,
    ):
        original = self.config_file.read_text()
        variants = [
            original.replace(
                '"worker_id": "fixture"',
                '"worker_id": "fixture", "worker_id": "fixture"',
            ),
            original.replace('"worker_id": "fixture"', '"worker_id": "other"'),
            json.dumps({**self.config, "secret-value": "private-secret"}),
            " " * (64 * 1024 + 1),
        ]
        with patch(
            "literate_ai.cli.worker_health.inspect_worker_storage",
            side_effect=AssertionError("probe must not run"),
        ):
            for raw in variants:
                self.config_file.write_text(raw)
                status, output = self.invoke("--json")
                self.assertNotEqual(status, 0)
                self.assertNotIn("private-secret", output)
                self.assertNotIn("secret-value", output)

    def test_explicit_command_receiver_runs_without_lifecycle_dispatch(self):
        from literate_ai import worker_storage_probe

        worker = ExecutionWorker(
            "fixture",
            ExecutionWorkerKind.COMMAND,
            requirements=ExecutionRequirements(os_family=self.family),
            command=("never-run-lifecycle", "{request_file}"),
        )
        self.catalog.write_text(json.dumps(ExecutionWorkerCatalog((worker,)).to_dict()))
        self.config["health_command"] = {
            "schema": "literate-ai/private-worker-storage-command@1",
            "command": [sys.executable, "-B", worker_storage_probe.__file__],
            "environment": [],
        }
        self.write_config()
        status, output = self.invoke("--json")
        self.assertEqual(status, 0, output)
        self.assertNotIn("never-run-lifecycle", output)
        self.assertNotIn(worker_storage_probe.__file__, output)
        self.assertTrue(
            all(
                sample["available_bytes"]["status"] == "measured"
                for sample in json.loads(output)["result"]["observation"]["samples"]
            )
        )

    def test_catalog_change_during_measurement_also_refuses(self):
        def observer(*args, **kwargs):
            observed = self.observed(*args, **kwargs)
            self.catalog.write_text("changed catalog")
            return observed

        with self.using_observer(observer):
            status, output = self.invoke("--json")
        self.assertNotEqual(status, 0)
        self.assertIn("worker.health_input_invalid", output)

    def test_explicit_alert_history_deduplicates_and_reports_recovery(self):
        from tests.unit.test_worker_capacity import NOW

        for role in self.config["capacity"]["roles"]:
            role["reserve_bytes"] = 100
        self.config["capacity"]["warning_headroom_bytes"] = 20
        self.write_config()
        state_path = self.root / "alerts.json"
        tick = 0
        available = 99

        def inspect(inputs, **kwargs):
            def observe(*args, **options):
                return replace(
                    self.observed(*args, **options, available=available),
                    started_at_ms=NOW + tick - 2,
                    completed_at_ms=NOW + tick - 1,
                    expires_at_ms=NOW + tick + 1000,
                )

            return inspect_worker_storage(
                inputs, observer=observe, clock_ms=lambda: NOW + tick, **kwargs
            )

        with patch(
            "literate_ai.cli.worker_health.inspect_worker_storage", side_effect=inspect
        ):
            status, first = self.invoke("--alert-state", str(state_path), "--json")
            self.assertEqual(status, 1)
            first = json.loads(first)["result"]
            self.assertEqual(len(first["events"]), 4)
            tick += 1
            _, repeated = self.invoke("--alert-state", str(state_path), terminal=True)
            self.assertIn("No new alert transitions", repeated)
            tick += 1
            available = 1000
            status, recovery = self.invoke("--alert-state", str(state_path), "--json")
            self.assertEqual(status, 0)
            recovered = json.loads(recovery)["result"]
            self.assertEqual(
                {event["transition"] for event in recovered["events"]}, {"recovered"}
            )
            self.assertEqual(recovered["assessment"]["decision"], "proceed")
        self.assertNotIn(str(self.root), recovery)
        self.assertLess(state_path.stat().st_size, 65536)

    def test_configuration_schema_and_capacity_schema_resolve_together(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        from literate_ai.schema_catalog import verify_schema_catalog

        schemas = Path(__file__).resolve().parents[2] / "schemas"
        registry = Registry()
        for path in schemas.glob("v*/*.schema.json"):
            document = json.loads(path.read_text())
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
        registry = registry.crawl()
        validator = Draft202012Validator(
            {"$ref": self.config["schema"]}, registry=registry
        )
        validator.validate(self.config)
        invalid = {
            **self.config,
            "capacity": {
                **self.config["capacity"],
                "storage_bindings_identity": "foreign",
            },
        }
        self.assertTrue(list(validator.iter_errors(invalid)))
        self.assertGreater(
            verify_schema_catalog("v2", schemas / "v2")["resource_count"], 0
        )

    def test_role_mismatch_and_foreign_binding_override_refuse(self):
        for changes in (
            {"paths": self.config["paths"][:-1]},
            {
                "capacity": {
                    **self.config["capacity"],
                    "storage_bindings_identity": "foreign",
                }
            },
        ):
            self.config_file.write_text(json.dumps({**self.config, **changes}))
            with patch(
                "literate_ai.cli.worker_health.inspect_worker_storage",
                side_effect=AssertionError("probe must not run"),
            ):
                status, output = self.invoke("--json")
            self.assertNotEqual(status, 0)
            self.assertIn("worker.health_input_invalid", output)

    def test_read_only_command_refuses_discovery_or_debug_file(self):
        for extra in (("--discover-mcps",), ("--debug", str(self.root / "debug.json"))):
            status, output = self.invoke(*extra, "--json")
            self.assertNotEqual(status, 0)
            self.assertIn("worker.health_read_only_options", output)
        self.assertFalse((self.root / "debug.json").exists())


if __name__ == "__main__":
    unittest.main()
