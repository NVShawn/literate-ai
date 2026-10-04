"""Private automatic INDEX selection reaches real health and command receivers."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai import worker_storage_probe
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
)
from literate_ai.adapters.action_execution_config import (
    ActionExecutionConfigurationError,
    load_action_execution,
)
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.standard_rebuild import FilesystemStandardRebuildError
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog
from tests.support import fixtures_test_cli_worker_health as health_fixture
from tests.support import fixtures_test_standard_action_indexing as factory_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_blob_source import source_cas_server


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
            deadline=ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE),
        )

    def test_source_cas_cannot_overlap_project_authority(self):
        for root in (self.project, self.project / "cas", self.project.parent):
            with self.subTest(root=root):
                self.configuration["source_cas_root"] = str(root)
                with self.assertRaisesRegex(
                    ActionExecutionConfigurationError, "outside project"
                ):
                    self.admit()

    def test_public_factory_automatically_uses_private_configuration(self):
        with source_cas_server(self.factory.admission.fixture.blobs) as (url, requests):
            self.configure(url)
            self.configuration["source_handoff"] = "http-cas"
            self.load()
            with patch.dict(os.environ, self.environment, clear=True):
                adapter = self.factory.assemble()
                indexer = adapter.runtime.application.lifecycle.indexer
                self.assertIsInstance(indexer, CommandGenerationIndexer)
                self.assertIsNotNone(adapter.action_execution)
                candidate = self.factory.register(adapter.runtime)
                result = indexer.index(
                    candidate.component_revision, candidate.tree_identity
                )
                self.assertIsNotNone(result)
                self.assertTrue(requests)
                self.path.write_bytes(b"{}")
                with self.assertRaises(FilesystemStandardRebuildError):
                    adapter._require_action_configuration()

    def test_public_factory_rejects_invalid_configuration_without_local_fallback(self):
        self.path.write_bytes(b"{}")
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaises(FilesystemStandardRebuildError):
                self.factory.assemble()


if __name__ == "__main__":
    unittest.main()
