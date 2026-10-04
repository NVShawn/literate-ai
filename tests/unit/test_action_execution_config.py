"""Private automatic INDEX selection reaches real health and command receivers."""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai import worker_storage_probe
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_execution_config import (
    ActionExecutionConfigurationError,
    BoundActionExecution,
    load_action_execution,
)
from literate_ai.adapters.command_builder import CommandComponentBuilder
from literate_ai.adapters.command_executor import CommandComponentExecutor
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.command_tester import CommandComponentTester
from literate_ai.adapters.local_test_handoff import LocalBuildTestHandoff
from literate_ai.adapters.retained_source import (
    RetainedSourceError,
    RetainedSourceInput,
)
from literate_ai.adapters.standard_rebuild import FilesystemStandardRebuildError
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.worker_capabilities import WorkerHardwareObservationCatalog
from tests.support import fixtures_test_cli_worker_health as health_fixture
from tests.support import fixtures_test_standard_action_indexing as factory_fixture
from tests.unit.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server


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

    def test_explicit_result_sources_fetch_verify_and_refuse_worker_or_config_drift(
        self,
    ):
        cas = self.factory.admission.fixture.controller_cas
        reference = cas.put_bytes(b"worker artifact")
        worker = self.factory.admission.source.worker
        self.environment["RESULT_TEST_TOKEN"] = "fixture-token"
        with source_cas_server({blob_path(reference): cas.path_for(reference)}) as (
            url,
            requests,
        ):
            self.configuration["result_sources"] = {
                worker.worker_id: {
                    "kind": "http-cas",
                    "endpoint": url,
                    "allow_http": True,
                    "token_env": "RESULT_TEST_TOKEN",
                }
            }
            bound = self.load()
            self.assertEqual(requests, [])
            fetch = bound.build_result_source(self.result_pool(), cas)
            self.assertEqual(fetch(worker, reference), b"worker artifact")
            self.assertEqual(requests[0][1], "Bearer fixture-token")
            with self.assertRaises(ActionWireError):
                fetch(replace(worker, target_profile="other"), reference)
            self.path.write_bytes(b"changed")
            with self.assertRaises(ActionExecutionConfigurationError):
                fetch(worker, reference)
            self.assertEqual(len(requests), 1)

    def test_shared_result_cas_requires_explicit_binding(self):
        cas = self.factory.admission.fixture.controller_cas
        pool = self.result_pool()
        with self.assertRaises(ActionExecutionConfigurationError) as raised:
            self.load().build_result_source(pool, cas)
        self.assertEqual(
            raised.exception.code, "action_execution.result_source_missing"
        )
        self.configuration["result_sources"] = {"index": {"kind": "shared-cas"}}
        fetch = self.load().build_result_source(pool, cas)
        reference = cas.put_bytes(b"shared artifact")
        self.assertEqual(fetch(pool.workers[0], reference), b"shared artifact")

    def test_result_transport_requires_typed_supported_phase_selection(self):
        bound = self.load()
        for phases in (
            (),
            ("test",),
            (LifecycleActionKind.INDEX,),
            (LifecycleActionKind.TEST, LifecycleActionKind.TEST),
        ):
            with self.subTest(phases=phases), self.assertRaises(ValueError):
                bound.build_result_source(
                    self.result_pool(),
                    self.factory.admission.fixture.controller_cas,
                    phases=phases,
                )

    def test_result_endpoint_and_credentials_are_validated_without_network(self):
        invalid = (
            {"kind": "shared-cas", "endpoint": "https://example.invalid"},
            {"kind": "http-cas", "endpoint": "http://example.invalid"},
            {
                "kind": "http-cas",
                "endpoint": "https://example.invalid",
                "token_env": "MISSING_TEST_RESULT_TOKEN",
            },
            {
                "kind": "http-cas",
                "endpoint": "https://example.invalid",
                "allow_http": 1,
            },
            {"kind": "http-cas", "endpoint": "https://user:password@example.invalid"},
        )
        self.environment.pop("MISSING_TEST_RESULT_TOKEN", None)
        for value in invalid:
            with self.subTest(value=value):
                self.configuration["result_sources"] = {"index": value}
                with self.assertRaises(ActionExecutionConfigurationError):
                    self.load()

    def test_absent_default_preserves_local_but_explicit_missing_refuses(self):
        self.assertIsNone(self.load(write=False))
        self.environment["LITAI_ACTION_EXECUTION_CONFIG"] = str(self.path)
        with self.assertRaisesRegex(ActionExecutionConfigurationError, "is missing"):
            self.load(write=False)
        self.environment["LITAI_ACTION_EXECUTION_CONFIG"] = "relative.json"
        with self.assertRaises(ActionExecutionConfigurationError):
            self.load(write=False)

    def test_unknown_duplicate_unbounded_and_relative_configuration_refuse(self):
        original = dict(self.configuration)
        for key, value in (
            ("extra", "private-secret"),
            ("duration_seconds", True),
            ("duration_seconds", 86401),
            ("maximum_hardware_age_seconds", 0),
            ("source_cas_root", "relative"),
            ("source_handoff", "unknown"),
            ("health_configurations", {}),
        ):
            with self.subTest(key=key, value=value):
                self.configuration = original | {key: value}
                with self.assertRaises(ActionExecutionConfigurationError) as caught:
                    self.load()
                self.assertNotIn("private-secret", str(caught.exception))
        self.path.write_bytes(b'{"schema":"one","schema":"two"}')
        with self.assertRaises(ActionExecutionConfigurationError):
            self.load(write=False)
        self.path.write_bytes(b"x" * 65537)
        with self.assertRaises(ActionExecutionConfigurationError):
            self.load(write=False)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_nonregular_configuration_refuses_without_waiting_for_a_writer(self):
        os.mkfifo(self.path)
        with self.assertRaises(ActionExecutionConfigurationError):
            self.load(write=False)

    def test_configuration_drift_refuses_before_admission(self):
        bound = self.load()
        self.path.write_bytes(b"{}")
        with self.assertRaisesRegex(ActionExecutionConfigurationError, "changed"):
            self.admit(bound)

    def test_action_budget_must_cover_bounded_health_probe_before_starting_it(self):
        self.configuration["duration_seconds"] = 1
        with patch(
            "literate_ai.adapters.action_execution_config.inspect_worker_storage"
        ) as inspect:
            with self.assertRaises(ActionWireError):
                self.admit()
            inspect.assert_not_called()

    def test_symbolic_configuration_refuses(self):
        actual = self.root / "actual.json"
        actual.write_bytes(canonical_json_bytes(self.configuration))
        try:
            self.path.symlink_to(actual)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links unavailable")
        with self.assertRaises(ActionExecutionConfigurationError):
            self.load(write=False)

    def test_source_cas_cannot_overlap_project_authority(self):
        for root in (self.project, self.project / "cas", self.project.parent):
            with self.subTest(root=root):
                self.configuration["source_cas_root"] = str(root)
                with self.assertRaisesRegex(
                    ActionExecutionConfigurationError, "outside project"
                ):
                    self.admit()

    def test_actual_health_and_capability_admission_rechecks_pinned_health_policy(self):
        bound = self.load()
        pool, cas = self.admit(bound)
        self.assertEqual(tuple(worker.worker_id for worker in pool.workers), ("index",))
        self.assertEqual(cas.root, self.factory.admission.fixture.controller_cas.root)
        self.health.config_file.write_text("{}")
        with self.assertRaises(ActionWireError):
            pool.revalidate(pool.workers[0])

    def test_stale_disk_hardware_is_replaced_by_live_collection(self):
        root = self.root / "new-source-cas"
        self.configuration["source_cas_root"] = str(root)
        stale = replace(
            self.factory.admission.observed,
            observed_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )
        self.observations.write_bytes(
            canonical_json_bytes(WorkerHardwareObservationCatalog((stale,)).to_dict())
        )
        pool, _ = self.admit()
        self.assertNotEqual(
            pool.hardware_observations.worker("index").observed_at, stale.observed_at
        )
        self.assertTrue(root.exists())

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

    def test_public_factory_automatically_installs_configured_build_result_transport(
        self,
    ):
        pool, cas = self.admit()
        worker = pool.workers[0]
        facts = pool._facts[worker.worker_id]
        pool._facts[worker.worker_id] = replace(
            facts,
            actions=tuple(sorted((*facts.actions, LifecycleActionKind.BUILD))),
            build_profile=canonical_identity("configured fixture"),
            build_toolchains=(canonical_identity("fixture compiler"),),
        )
        self.configuration["result_sources"] = {"index": {"kind": "shared-cas"}}
        self.load()
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(BoundActionExecution, "admit", return_value=(pool, cas)),
        ):
            adapter = self.factory.assemble()
        builder = adapter.runtime.application.lifecycle.builder
        self.assertIsInstance(builder, CommandComponentBuilder)
        reference = cas.put_bytes(b"automatic result transport")
        self.assertEqual(
            builder.result_source(pool.catalog.worker(worker.worker_id), reference),
            b"automatic result transport",
        )

    def test_public_factory_composes_test_with_local_or_remote_build(self):
        pool, cas = self.admit()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        self.configuration["result_sources"] = {"index": {"kind": "shared-cas"}}
        self.load()
        for remote_build in (False, True):
            facts = replace(
                original,
                actions=tuple(
                    sorted(
                        (
                            *original.actions,
                            LifecycleActionKind.TEST,
                            *((LifecycleActionKind.BUILD,) if remote_build else ()),
                        )
                    )
                ),
                test_profile=canonical_identity("test"),
                test_toolchains=(canonical_identity("runner"),),
                build_profile=canonical_identity("build") if remote_build else None,
                build_toolchains=(canonical_identity("compiler"),)
                if remote_build
                else (),
            )
            pool._facts[worker.worker_id] = facts
            with (
                self.subTest(remote_build=remote_build),
                patch.dict(os.environ, self.environment, clear=True),
                patch.object(BoundActionExecution, "admit", return_value=(pool, cas)),
            ):
                adapter = self.factory.assemble()
            lifecycle = adapter.runtime.application.lifecycle
            self.assertIsInstance(lifecycle.tester, CommandComponentTester)
            self.assertIsInstance(
                lifecycle.builder,
                CommandComponentBuilder if remote_build else LocalBuildTestHandoff,
            )
            self.assertIs(lifecycle.tester.indexer, lifecycle.indexer)
            self.assertIs(lifecycle.tester.handoff_for.__self__, lifecycle.builder)
            reference = cas.put_bytes(b"TEST result")
            self.assertEqual(
                lifecycle.tester.result_source(
                    pool.catalog.worker(worker.worker_id), reference
                ),
                b"TEST result",
            )

    def test_public_factory_composes_execute_with_all_build_test_placements(self):
        pool, cas = self.admit()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        self.configuration["result_sources"] = {"index": {"kind": "shared-cas"}}
        self.load()
        for remote_build in (False, True):
            for remote_test in (False, True):
                pool._facts[worker.worker_id] = replace(
                    original,
                    actions=tuple(
                        sorted(
                            (
                                *original.actions,
                                LifecycleActionKind.EXECUTE,
                                *((LifecycleActionKind.BUILD,) if remote_build else ()),
                                *((LifecycleActionKind.TEST,) if remote_test else ()),
                            )
                        )
                    ),
                    execute_profile=canonical_identity("execute"),
                    execute_toolchains=(canonical_identity("runtime"),),
                    test_profile=canonical_identity("test") if remote_test else None,
                    test_toolchains=(canonical_identity("runner"),)
                    if remote_test
                    else (),
                    build_profile=canonical_identity("build") if remote_build else None,
                    build_toolchains=(canonical_identity("compiler"),)
                    if remote_build
                    else (),
                )
                with (
                    self.subTest(build=remote_build, test=remote_test),
                    patch.dict(os.environ, self.environment, clear=True),
                    patch.object(
                        BoundActionExecution, "admit", return_value=(pool, cas)
                    ),
                ):
                    lifecycle = self.factory.assemble().runtime.application.lifecycle
                self.assertIsInstance(lifecycle.executor, CommandComponentExecutor)
                self.assertIsInstance(
                    lifecycle.builder,
                    CommandComponentBuilder if remote_build else LocalBuildTestHandoff,
                )
                self.assertIs(lifecycle.executor.indexer, lifecycle.indexer)
                self.assertIs(lifecycle.executor.handoff_for.builder, lifecycle.builder)
                if not remote_build:
                    self.assertNotIsInstance(
                        lifecycle.builder.builder, LocalBuildTestHandoff
                    )
                self.assertEqual(
                    isinstance(lifecycle.tester, CommandComponentTester), remote_test
                )
                reference = cas.put_bytes(b"EXECUTE result")
                self.assertEqual(
                    lifecycle.executor.result_source(
                        pool.catalog.worker(worker.worker_id), reference
                    ),
                    b"EXECUTE result",
                )

    def test_execute_only_capability_requires_configured_return_transport(self):
        pool, cas = self.admit()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        pool._facts[worker.worker_id] = replace(
            original,
            actions=tuple(sorted((*original.actions, LifecycleActionKind.EXECUTE))),
            execute_profile=canonical_identity("execute"),
            execute_toolchains=(canonical_identity("runtime"),),
        )
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(BoundActionExecution, "admit", return_value=(pool, cas)),
            self.assertRaises(FilesystemStandardRebuildError) as error,
        ):
            self.factory.assemble()
        self.assertEqual(error.exception.code, "action_execution.result_source_missing")

    def test_test_only_capability_requires_configured_return_transport(self):
        pool, cas = self.admit()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        pool._facts[worker.worker_id] = replace(
            original,
            actions=tuple(sorted((*original.actions, LifecycleActionKind.TEST))),
            test_profile=canonical_identity("test"),
            test_toolchains=(canonical_identity("runner"),),
        )
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(BoundActionExecution, "admit", return_value=(pool, cas)),
        ):
            with self.assertRaises(FilesystemStandardRebuildError) as error:
                self.factory.assemble()
        self.assertEqual(error.exception.code, "action_execution.result_source_missing")

    def test_public_factory_rejects_invalid_configuration_without_local_fallback(self):
        self.path.write_bytes(b"{}")
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaises(FilesystemStandardRebuildError):
                self.factory.assemble()

    def test_retained_source_authorization_precedes_automatic_worker_admission(self):
        retained = RetainedSourceInput.capture(
            self.factory.admission.fixture.source_root,
            component_lock_identity=self.factory.snapshot.authority.lock.identity,
            project_authority_identity=canonical_identity("validated-project"),
            target="host",
        )
        with patch(
            "literate_ai.adapters.standard_rebuild.load_action_execution"
        ) as load:
            with self.assertRaises(RetainedSourceError):
                self.factory.assemble(retained_source=retained)
            load.assert_not_called()

    def test_missing_disk_observations_do_not_block_live_admission(self):
        self.observations.unlink()
        pool, _ = self.admit()
        self.assertEqual(tuple(worker.worker_id for worker in pool.workers), ("index",))
        self.assertFalse(self.observations.exists())

    def test_unavailable_live_hardware_refuses_before_source_cas_allocation(self):
        root = self.root / "new-cas"
        self.configuration["source_cas_root"] = str(root)
        for failure, expected in (
            (
                ActionWireError("test.unavailable", "private-endpoint-secret"),
                "test.unavailable",
            ),
            (OSError("private-path-secret"), "action_execution.hardware_os_error"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch(
                    "literate_ai.adapters.action_execution_config.probe_command_hardware",
                    side_effect=failure,
                ),
            ):
                with self.assertRaises(ActionWireError) as caught:
                    self.admit()
                self.assertEqual(
                    caught.exception.code, "action_execution.hardware_probe_failed"
                )
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertFalse(root.exists())

    def test_live_hardware_cache_refreshes_expiry_and_retains_stable_facts(self):
        observed = self.factory.admission.observed
        with patch(
            "literate_ai.adapters.action_execution_config.probe_command_hardware",
            return_value=observed,
        ) as probe:
            pool, _ = self.admit()
            pool.revalidate(pool.workers[0])
            self.assertEqual(probe.call_count, 1)
            future = datetime.fromisoformat(observed.observed_at) + timedelta(
                seconds=301
            )
            refreshed = replace(observed, observed_at=future.isoformat())
            probe.return_value = refreshed
            with patch(
                "literate_ai.adapters.action_execution_config.datetime"
            ) as clock:
                clock.now.return_value = future
                clock.fromisoformat = datetime.fromisoformat
                current = pool.observations_loader()
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(current.worker("index"), refreshed)

    def test_legacy_and_wrong_target_candidates_are_not_probed(self):
        original = self.factory.admission.source.worker
        for worker in (
            replace(original, action_protocol=None),
            replace(original, target_profile="other"),
        ):
            with self.subTest(worker=worker.worker_id):
                self.catalog.write_bytes(
                    canonical_json_bytes(ExecutionWorkerCatalog((worker,)).to_dict())
                )
                with patch(
                    "literate_ai.adapters.action_execution_config.probe_command_hardware"
                ) as probe:
                    with self.assertRaises(ActionWireError):
                        self.admit()
                probe.assert_not_called()

    def test_unavailable_candidate_does_not_prevent_live_healthy_admission(self):
        worker = self.factory.admission.source.worker
        unavailable = replace(worker, worker_id="bad")
        self.catalog.write_bytes(
            canonical_json_bytes(
                ExecutionWorkerCatalog((unavailable, worker)).to_dict()
            )
        )
        self.configuration["health_configurations"]["bad"] = str(
            self.health.config_file
        )
        pool, _ = self.admit()
        self.assertEqual(tuple(item.worker_id for item in pool.workers), ("index",))
        self.assertEqual(pool.refusals, (("bad", "action_admission.not_eligible"),))


if __name__ == "__main__":
    unittest.main()
