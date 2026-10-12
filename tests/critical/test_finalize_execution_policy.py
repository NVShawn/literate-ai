"""Configured FINALIZE policy stays pinned, live and never falls back to local runs."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.fixtures_test_cli_worker_health as health_fixture
import tests.support.fixtures_test_standard_action_indexing as factory_fixture
from literate_ai import worker_storage_probe
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
)
from literate_ai.adapters.action_execution_config import (
    ActionExecutionConfigurationError,
    BoundActionExecution,
    load_action_execution,
)
from literate_ai.adapters.standard_rebuild import FilesystemStandardRebuildError
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog


class ActionExecutionConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.factory = factory_fixture.StandardActionIndexingTests()
        self.factory.setUp()
        self.addCleanup(self.factory.doCleanups)
        self.health = health_fixture.WorkerHealthCliTests()
        self.health.setUp()
        self.addCleanup(self.health.doCleanups)
        self.root = self.factory.rebuild.root
        self.project = self.factory.rebuild.project_root
        self.path = self.root / "action-execution.json"
        self.catalog = self.root / "workers.json"
        self.observations = self.root / "worker-observations.json"
        self.environment = dict(os.environ) | {
            "LITAI_CONFIG_DIR": str(self.root),
            "LITAI_STATE_DIR": str(self.root),
            "LITAI_WORKER_CONFIG": str(self.catalog),
            "LITAI_WORKER_OBSERVATIONS": str(self.observations),
        }
        self.environment.pop("LITAI_ACTION_EXECUTION_CONFIG", None)
        self.configuration = {
            "schema": "literate-ai/private-action-execution@1",
            "source_cas_root": str(self.factory.admission.fixture.controller_cas.root),
            "source_handoff": "filesystem-cas",
            "duration_seconds": 300,
            "maximum_hardware_age_seconds": 300,
            "health_configurations": {"index": str(self.health.config_file)},
        }
        self.configure()

    def configure(self, url=None):
        self.factory.admission.configure(url)
        worker = self.factory.admission.source.worker
        self.catalog.write_bytes(
            canonical_json_bytes(ExecutionWorkerCatalog((worker,)).to_dict())
        )
        self.observations.write_bytes(
            canonical_json_bytes(
                WorkerHardwareObservationCatalog(
                    (self.factory.admission.observed,)
                ).to_dict()
            )
        )
        self.environment["INDEX_WORKER_IDENTITY"] = worker.identity.uri
        self.health.config["worker_id"] = worker.worker_id
        self.health.config["health_command"] = {
            "schema": "literate-ai/private-worker-storage-command@1",
            "command": [sys.executable, "-B", worker_storage_probe.__file__],
            "environment": [],
        }
        self.health.write_config()

    def load(self, *, write=True):
        if write:
            self.path.write_bytes(canonical_json_bytes(self.configuration))
        return load_action_execution(
            project_root=self.project, environment=self.environment
        )

    def admit(self, bound=None):
        return (bound or self.load()).admit(
            project_root=self.project,
            target_profile="host",
            job_identity=canonical_identity("configuration-job"),
        )

    def result_pool(self):
        worker = self.factory.admission.source.worker
        return SimpleNamespace(
            catalog=ExecutionWorkerCatalog((worker,)),
            workers=(worker,),
            supports_phase=lambda worker, phase: True,
            deadline=ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=1)),
        )

    def test_persisted_finalize_policy_is_closed_pinned_and_live(self):
        profile = canonical_identity("configured-finalize-profile")
        policy = {"profile_identity": profile.uri, "verifier": "portable-application@1"}
        self.configuration["finalize"] = policy
        bound = self.load()
        self.assertEqual(bound.finalize_profile, profile)
        bound.require_unchanged()
        self.configuration["finalize"] = policy | {
            "profile_identity": canonical_identity("changed-profile").uri
        }
        self.load()
        with self.assertRaises(ActionExecutionConfigurationError) as error:
            bound.require_unchanged()
        self.assertEqual(error.exception.code, "action_execution.configuration_changed")
        for invalid in (
            None,
            {},
            policy | {"verifier": "arbitrary.module:callback"},
            policy | {"profile_identity": "invalid"},
            policy | {"extra": True},
        ):
            with self.subTest(policy=invalid):
                self.configuration["finalize"] = invalid
                with self.assertRaises(ActionExecutionConfigurationError) as error:
                    self.load()
                self.assertEqual(
                    error.exception.code, "action_execution.configuration_invalid"
                )

    def test_configured_finalize_policy_cannot_silently_use_local_execution(self):
        pool, cas = self.admit()
        self.configuration["finalize"] = {
            "profile_identity": canonical_identity("finalize-profile").uri,
            "verifier": "portable-application@1",
        }
        self.load()
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(BoundActionExecution, "admit", return_value=(pool, cas)),
            self.assertRaises(FilesystemStandardRebuildError) as error,
        ):
            self.factory.assemble()
        self.assertEqual(error.exception.code, "standard_rebuild.finalize_unavailable")


if __name__ == "__main__":
    unittest.main()
