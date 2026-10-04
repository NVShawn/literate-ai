from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_standard_action_indexing``."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_source_index import MAX_SOURCE_BYTES
from literate_ai.adapters.command_builder import CommandComponentBuilder
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.generation_preparation import (
    LockedComponentModelSelectionAdapter,
)
from literate_ai.adapters.intelligence import DisabledGenerationIndexer
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectRuntime,
    PlannedStandardProject,
    StandardProjectRuntimeReadiness,
)
from literate_ai.adapters.standard_rebuild import (
    FilesystemStandardRebuildError,
    FilesystemStandardRebuildRequest,
    assemble_filesystem_standard_rebuild_adapter,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_action_admission as admission_fixture
from tests.support import fixtures_test_standard_rebuild_adapter as rebuild_fixture
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_project_factory import (
    _command_contracts,
    _selection,
    _toolchain_closure,
)
from tests.unit.standard_source_evidence_fixture import register_strict_source


class _Generator:
    def __call__(self, _prepared):
        raise AssertionError("this fixture registers generated source explicitly")

    def planned_cache_key(self, _prepared):
        return canonical_identity("source-cache-key")

    def cache_key_for_candidate(self, _candidate):
        return canonical_identity("source-cache-key")


class StandardActionIndexingTests(unittest.TestCase):
    def setUp(self):
        self.admission = admission_fixture.CommandActionAdmissionTests()
        self.admission.setUp()
        self.addCleanup(self.admission.doCleanups)
        self.rebuild = rebuild_fixture.FilesystemStandardRebuildAdapterTests()
        self.rebuild.setUp()
        self.addCleanup(self.rebuild.doCleanups)
        self.snapshot, self.execution = _fixture()
        contracts, tools = _command_contracts(self.execution)
        self.closure = _toolchain_closure(self.execution, contracts, tools)
        self.generator = _Generator()
        self.source_generation = SimpleNamespace(
            generator=SimpleNamespace(selection=_selection()),
            model_selector=LockedComponentModelSelectionAdapter(),
            runner=lambda *args, **kwargs: (self.generator, None),
        )

    def assemble(self, **changes):
        root = self.rebuild.root
        arguments = dict(
            project=self.rebuild.project,
            prepared=replace(
                self.rebuild.prepared, locked_authority_snapshot=self.snapshot
            ),
            object_root=root / "objects",
            generated_source_cache_root=root / "generated-cache",
            source_cache_root=root / "source-cache",
            candidate_cas_root=root / "candidate-cas",
            accepted_cas_root=root / "accepted-cas",
            checkpoint_root=root / "checkpoints",
            binding=self.rebuild.binding,
            authority_validator=lambda _root: canonical_identity("validated-project"),
        )
        arguments.update(changes)
        with (
            patch(
                "literate_ai.adapters.standard_rebuild.FilesystemStandardSourceGenerationAdapter.from_environment",
                return_value=self.source_generation,
            ),
            patch(
                "literate_ai.adapters.standard_rebuild.FilesystemStandardProjectPlanningAdapter.plan",
                return_value=PlannedStandardProject(_selection(), self.execution),
            ),
            patch(
                "literate_ai.adapters.standard_rebuild.project_locked_standard_toolchain_closure",
                return_value=self.closure,
            ),
            patch(
                "literate_ai.adapters.standard_rebuild.load_shared_cache",
                return_value=None,
            ),
        ):
            return assemble_filesystem_standard_rebuild_adapter(**arguments)

    def register(self, runtime):
        root = self.admission.fixture.source_root
        plan = self.execution.generation_plans[0]
        candidate = register_strict_source(
            runtime.source_trees,
            root,
            snapshot=self.snapshot,
            generation_plan=plan,
            identity_namespace="command-factory",
        )
        blobs = self.admission.fixture.blobs
        blobs.clear()
        for path in root.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                reference = BlobRef(record_identity(content).digest, len(content))
                blobs[blob_path(reference)] = (
                    self.admission.fixture.controller_cas.path_for(reference)
                )
        return candidate

    def test_factory_installs_build_only_with_explicit_result_transport(self):
        self.admission.configure()
        pool = self.admission.pool()
        worker = pool.workers[0]
        facts = pool._facts[worker.worker_id]
        pool._facts[worker.worker_id] = replace(
            facts,
            actions=tuple(sorted((*facts.actions, LifecycleActionKind.BUILD))),
            build_profile=canonical_identity("configured BUILD fixture"),
            build_toolchains=(canonical_identity("fixture compiler"),),
        )
        cas = self.admission.fixture.controller_cas
        with self.assertRaises(FilesystemStandardRebuildError) as raised:
            self.assemble(action_workers=pool, action_source_cas=cas)
        self.assertEqual(
            raised.exception.code, "standard_rebuild.result_source_missing"
        )

        def fetch(worker, ref):
            return cas.get_bytes(ref)

        adapter = self.assemble(
            action_workers=pool, action_source_cas=cas, action_result_source=fetch
        )
        lifecycle = adapter.runtime.application.lifecycle
        self.assertIsInstance(lifecycle.builder, CommandComponentBuilder)
        self.assertIs(lifecycle.builder.indexer, lifecycle.indexer)
        self.assertIs(lifecycle.builder.result_source, fetch)
        self.assertIs(lifecycle.builder.local_ports, adapter.runtime.lifecycle_ports)

    def test_factory_keeps_command_indexing_through_cache_and_checkpoints(
        self,
    ):
        with source_cas_server(self.admission.fixture.blobs) as (url, requests):
            self.admission.configure(url)
            pool = self.admission.pool(source_handoff="http-cas")
            adapter = self.assemble(
                action_workers=pool,
                action_source_cas=self.admission.fixture.controller_cas,
            )
            runtime = adapter.runtime
            indexer = runtime.application.lifecycle.indexer
            self.assertIsInstance(indexer, CommandGenerationIndexer)
            self.assertIs(adapter.action_workers, pool)
            self.assertIsNotNone(runtime.source_cache_restorer)
            self.assertIs(
                runtime.source_cache_restorer.source_trees, runtime.source_trees
            )
            self.assertIsNotNone(runtime.checkpoint_store)
            candidate = self.register(runtime)
            recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=1)
            indexer.retain_evidence_with(recorder)
            with patch(
                "literate_ai.adapters.lifecycle.standard_local.local_generated_source_tree_identity",
                side_effect=AssertionError(
                    "unbounded registry rehash before publication"
                ),
            ):
                identity = indexer.index(
                    candidate.component_revision, candidate.tree_identity
                )
            self.assertEqual(recorder.entries[0][0], identity)
            self.assertTrue(requests)
            self.assertEqual(
                runtime.source_trees.evidence(candidate.tree_identity).candidate,
                candidate,
            )

    def test_missing_strict_custody_refuses_before_publication_or_dispatch(self):
        self.admission.configure()
        pool = self.admission.pool()
        adapter = self.assemble(
            action_workers=pool, action_source_cas=self.admission.fixture.controller_cas
        )
        registry = adapter.runtime.source_trees
        candidate = self.admission.source.candidate
        registry.register(candidate, self.admission.fixture.source_root)
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "strict evidence custody"
        ):
            adapter.runtime.application.lifecycle.indexer.index(
                candidate.component_revision, candidate.tree_identity
            )
        self.assertTrue(
            all(not path.exists() for path in self.admission.fixture.blobs.values())
        )
        self.assertEqual(len(self.admission.health_checks), 1)

    def test_changed_registered_source_refuses_without_worker_dispatch(self):
        self.admission.configure()
        pool = self.admission.pool()
        adapter = self.assemble(
            action_workers=pool, action_source_cas=self.admission.fixture.controller_cas
        )
        candidate = self.register(adapter.runtime)
        (self.admission.fixture.source_root / "README.md").write_text("changed")
        with self.assertRaisesRegex(ActionWireError, "snapshot differs"):
            adapter.runtime.application.lifecycle.indexer.index(
                candidate.component_revision, candidate.tree_identity
            )
        self.assertEqual(len(self.admission.health_checks), 2)

    def test_source_bound_applies_before_any_registry_rehash_or_cas_publication(self):
        self.admission.configure()
        pool = self.admission.pool()
        adapter = self.assemble(
            action_workers=pool, action_source_cas=self.admission.fixture.controller_cas
        )
        candidate = self.register(adapter.runtime)
        with (
            patch(
                "literate_ai.adapters.lifecycle.standard_local.local_generated_source_tree_identity",
                side_effect=AssertionError("unbounded read"),
            ),
            patch("literate_ai.adapters.command_indexer.MAX_SOURCE_BYTES", 1),
            patch(
                "literate_ai.adapters.action_command_dispatch.CommandLifecycleActionDispatcher.dispatch"
            ) as dispatch,
        ):
            with self.assertRaises(RuntimeError):
                adapter.runtime.application.lifecycle.indexer.index(
                    candidate.component_revision, candidate.tree_identity
                )
            dispatch.assert_not_called()
        self.assertTrue(
            all(not path.exists() for path in self.admission.fixture.blobs.values())
        )

    def test_growth_during_worker_execution_refuses_before_result_retention(self):
        with source_cas_server(self.admission.fixture.blobs) as (url, _requests):
            self.admission.configure(url)
            pool = self.admission.pool(source_handoff="http-cas")
            adapter = self.assemble(
                action_workers=pool,
                action_source_cas=self.admission.fixture.controller_cas,
            )
            candidate = self.register(adapter.runtime)
            indexer = adapter.runtime.application.lifecycle.indexer
            recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=1)
            indexer.retain_evidence_with(recorder)
            original = indexer._dispatcher

            def dispatcher(records, results):
                delegate = original(records, results)
                dispatch = delegate.dispatch

                def changed(request):
                    result = dispatch(request)
                    with (self.admission.fixture.source_root / "growth.bin").open(
                        "wb"
                    ) as stream:
                        stream.truncate(MAX_SOURCE_BYTES + 1)
                    return result

                delegate.dispatch = changed
                return delegate

            indexer._dispatcher = dispatcher
            with (
                patch(
                    "literate_ai.adapters.lifecycle.standard_local.local_generated_source_tree_identity",
                    side_effect=AssertionError("unbounded revalidation"),
                ),
                self.assertRaises(RuntimeError),
            ):
                indexer.index(candidate.component_revision, candidate.tree_identity)
            self.assertEqual(recorder.entries, ())

    def test_partial_configuration_refuses_and_local_default_is_preserved(self):
        with self.assertRaisesRegex(
            FilesystemStandardRebuildError, "both admitted workers"
        ):
            self.assemble(action_source_cas=self.admission.fixture.controller_cas)
        adapter = self.assemble()
        self.assertIsInstance(
            adapter.runtime.application.lifecycle.indexer, DisabledGenerationIndexer
        )
        self.assertIsNone(adapter.action_workers)

    def test_pool_for_another_locked_target_refuses(self):
        self.admission.configure()
        worker = replace(self.admission.source.worker, target_profile="other")
        self.admission.source.worker = worker
        self.admission.catalog = ExecutionWorkerCatalog((worker,))
        pool = self.admission.pool(target_profile="other")
        with self.assertRaisesRegex(
            FilesystemStandardRebuildError, "different locked target"
        ):
            self.assemble(
                action_workers=pool,
                action_source_cas=self.admission.fixture.controller_cas,
            )

    def test_receipt_invocation_distinguishes_command_admission_from_local_indexing(
        self,
    ):
        self.admission.configure()
        pool = self.admission.pool()
        adapters = (
            self.assemble(),
            self.assemble(
                action_workers=pool,
                action_source_cas=self.admission.fixture.controller_cas,
            ),
        )
        contexts = []

        class ProjectionReached(Exception):
            pass

        def capture(_lifecycle, **context):
            contexts.append(context)
            raise ProjectionReached()

        request = FilesystemStandardRebuildRequest(
            replace(self.rebuild.prepared, locked_authority_snapshot=self.snapshot),
            self.rebuild.source_root,
            self.rebuild.invalidation,
        )
        with (
            patch.object(
                FilesystemStandardProjectRuntime,
                "plan",
                return_value=PlannedStandardProject(_selection(), self.execution),
            ),
            patch.object(
                FilesystemStandardProjectRuntime,
                "production_readiness",
                return_value=StandardProjectRuntimeReadiness(True, ()),
            ),
            patch.object(
                FilesystemStandardProjectRuntime,
                "execute",
                return_value=SimpleNamespace(
                    lifecycle=SimpleNamespace(successful=True)
                ),
            ),
            patch(
                "literate_ai.adapters.standard_rebuild.project_standard_project_test_receipt",
                side_effect=capture,
            ),
        ):
            for adapter in adapters:
                with self.assertRaises(ProjectionReached):
                    adapter.rebuild(request)
        self.assertEqual(
            contexts[0]["project_revision_identity"],
            contexts[1]["project_revision_identity"],
        )
        self.assertNotEqual(
            contexts[0]["lifecycle_invocation_identity"],
            contexts[1]["lifecycle_invocation_identity"],
        )
        self.assertNotEqual(
            contexts[0]["lifecycle_request_identity"],
            contexts[1]["lifecycle_request_identity"],
        )


if __name__ == "__main__":
    unittest.main()
