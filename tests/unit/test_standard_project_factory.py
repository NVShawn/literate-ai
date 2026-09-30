"""Public filesystem composition-root tests for Standard locked projects."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.candidate_repair import (
    FilesystemStandardCandidateRepairAdapter,
)
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    RegisteredSourceGenerationRunner,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.adapters.models import CodingCliSelection
from literate_ai.adapters.standard_project import (
    _STANDARD_BUILD_DRIVER,
    _STANDARD_NODE_RUNTIME_DRIVER,
    _STANDARD_PYTHON_RUNTIME_DRIVER,
    FilesystemStandardProjectPlanningAdapter,
    PlannedStandardProject,
    StandardAcceptedSourceContinuationError,
    StandardProjectExecutionRequest,
    StandardProjectPublicationRequest,
    _encoded_toolchain_environment,
    assemble_filesystem_standard_project_runtime,
    compose_filesystem_standard_lifecycle_checkpoints,
    project_standard_toolchain_closure,
)
from literate_ai.application.source_generation_scheduling import (
    ComponentSourceGenerationRunError,
)
from literate_ai.contracts import (
    AuthoredBinaryAsset,
    BlobRef,
    ComponentArtifactExportShape,
    ComponentChangeSurface,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentInvalidationDecision,
    ComponentLifecycleCommand,
    DependencyInputKind,
    StandardSourceSelector,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardToolchainClosure,
    canonical_identity,
)
from literate_ai.security import SecurityProfile
from tests.unit.test_component_node_generation_preparation import _budget, _fixture
from tests.unit.test_schema_catalog import SchemaCatalog


def _selection() -> CodingCliSelection:
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return CodingCliSelection("codex", str(executable), f"sha256:{digest}")


class _CompleteCachePublisher:
    def publish_accepted(self, publication):
        return publication.membership.identity


class _CacheRestorer:
    def restore_prepared(self, _execution_plan, _prepared, *, force_regeneration=False):
        return None


class _Indexer:
    def index(self, _component_revision, source):
        return source


class _CheckpointStore:
    def record(self, _evidence):
        return None

    def start_attempt(self, _execution_plan, _prepared_nodes):
        return None

    def restore_prepared(self, _execution_plan, _prepared):
        return None


def _command_contracts(execution):
    binding = LocalComponentToolBinding(sys.executable)
    commands = (
        ComponentLifecycleCommand(
            ComponentCommandPhase.BUILD,
            (
                "{tool}",
                "-c",
                "pass",
                "{source_root}",
                "{object_root}",
                "{export_path}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            ("{tool}", "-c", "pass", "{artifact_root}"),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.EXECUTE,
            ("{tool}", "-c", "pass", "{artifact_root}"),
        ),
    )
    contracts = tuple(
        ComponentCommandContract(
            component_revision=plan.component_revision,
            locked_build_authority_identity=canonical_identity(
                {"build-authority": plan.component_revision.uri}
            ),
            build_system_resolver_identity=canonical_identity({"resolver": "test"}),
            build_system_toolchain_identity=binding.toolchain_identity,
            language_compiler_identity=binding.toolchain_identity,
            language_runtime_identity=binding.toolchain_identity,
            commands=commands,
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, binding.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
            artifact_export=ComponentArtifactExportShape(
                f"artifact-{plan.component_revision.digest[:12]}",
                "executable",
                canonical_identity({"abi": "test"}),
                canonical_identity({"target": "test"}),
                "application/vnd.literate-ai.executable",
                canonical_identity({"producer": "test"}),
            ),
        )
        for plan in execution.generation_plans
    )
    return contracts, (binding,)


def _toolchain_closure(execution, contracts, tool_bindings):
    by_revision = {item.component_revision.uri: item for item in contracts}
    provider_exports = sorted(
        {
            by_revision[edge.provider_revision.uri].artifact_export.export_id
            for action_plan in execution.action_plans
            for edge in action_plan.dependency_edges
            if edge.semantics.consumed_input is DependencyInputKind.ARTIFACT_EXPORT
        }
    )
    provider_environment = {
        export_id: (f"LITAI_PROVIDER_{index}", "artifact-manifest.json")
        for index, export_id in enumerate(provider_exports)
    }
    observation = HostDependencyObservation(
        (
            {
                "type": "application",
                "bom-ref": "toolchain:test-python",
                "name": "test-python",
            },
        ),
        (("component:test-root", "toolchain:test-python"),),
    )
    return project_standard_toolchain_closure(
        execution,
        contracts=contracts,
        tool_bindings=tool_bindings,
        provider_environment=provider_environment,
        dependency_observation=observation,
        observer_identity=canonical_identity({"observer": "test-host-closure@1"}),
    )


class StandardProjectFactoryTests(unittest.TestCase):
    def test_cpp_build_driver_compiles_every_translation_unit_with_source_include(
        self,
    ) -> None:
        compiler = next(
            (
                command
                for name in ("c++", "g++", "clang++")
                if (command := shutil.which(name)) is not None
            ),
            None,
        )
        if compiler is None:
            self.skipTest("a portable C++ compiler is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            source = root / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            (source / "application.hpp").write_text(
                "#pragma once\nint application_value();\nint generated_tests();\n",
                encoding="utf-8",
            )
            (source / "application.cpp").write_text(
                '#include "application.hpp"\nint application_value() { return 7; }\n',
                encoding="utf-8",
            )
            (tests / "litai_test.cpp").write_text(
                '#include "application.hpp"\n'
                "int generated_tests() { return application_value() == 7 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            (source / "main.cpp").write_text(
                '#include "application.hpp"\n'
                "int main() { return generated_tests(); }\n",
                encoding="utf-8",
            )
            objects = Path(temporary) / "objects"
            executable = Path(temporary) / "artifact" / "run"

            subprocess.run(
                (
                    sys.executable,
                    "-c",
                    _STANDARD_BUILD_DRIVER,
                    "cpp-executable",
                    json.dumps([compiler]),
                    _encoded_toolchain_environment(()),
                    str(root),
                    "source/main.cpp",
                    str(objects),
                    str(executable),
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            completed = subprocess.run(
                (str(executable),),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0)

    def test_node_runtime_driver_executes_artifact_as_main_program(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        lifecycle_command = ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            (
                "{tool}",
                "-e",
                _STANDARD_NODE_RUNTIME_DRIVER,
                "{artifact_root}",
                "{export_path}",
                "tree",
                "source/main.js",
                "--litai-test",
            ),
        )
        self.assertIn("{artifact_root}", lifecycle_command.argv)
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "artifact"
            source = artifact / "source"
            source.mkdir(parents=True)
            (source / "main.js").write_text(
                "if (require.main !== module) throw new Error('not main');\n"
                "process.stdout.write(JSON.stringify("
                "{argv: process.argv.slice(2)}));\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                (
                    node,
                    "-e",
                    _STANDARD_NODE_RUNTIME_DRIVER,
                    str(artifact),
                    str(artifact),
                    "tree",
                    "source/main.js",
                    "--litai-test",
                ),
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertEqual(completed.stdout, '{"argv":["--litai-test"]}')

    def test_python_runtime_driver_imports_artifact_local_support_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "artifact"
            source = artifact / "source"
            support = source / "support"
            support.mkdir(parents=True)
            (support / "__init__.py").write_text(
                "VALUE = 'artifact-local'\n", encoding="utf-8"
            )
            (source / "main.py").write_text(
                "from support import VALUE\nprint(VALUE)\n", encoding="utf-8"
            )

            completed = subprocess.run(
                (
                    sys.executable,
                    "-c",
                    _STANDARD_PYTHON_RUNTIME_DRIVER,
                    str(artifact),
                    str(artifact),
                    "tree",
                    "source/main.py",
                ),
                text=True,
                capture_output=True,
                check=True,
            )

            artifact_files = tuple(
                path.relative_to(artifact).as_posix()
                for path in sorted(artifact.rglob("*"))
                if path.is_file()
            )

        self.assertEqual(completed.stdout.strip(), "artifact-local")
        self.assertEqual(
            artifact_files,
            ("source/main.py", "source/support/__init__.py"),
        )

    def test_projected_toolchain_closure_binds_plan_flavors_commands_and_providers(
        self,
    ) -> None:
        _snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)

        closure = _toolchain_closure(execution, contracts, tool_bindings)

        self.assertEqual(
            closure.record.component_lock_identity,
            execution.component_lock_identity,
        )
        self.assertEqual(closure.record.execution_plan_identity, execution.identity)
        self.assertEqual(
            tuple(
                item.component_revision for item in closure.record.component_authorities
            ),
            tuple(item.component_revision for item in execution.generation_plans),
        )
        self.assertEqual(
            tuple(
                item.flavor_selection_identity
                for item in closure.record.component_authorities
            ),
            tuple(
                item.generation_key.flavor_selection_identity
                for item in execution.generation_plans
            ),
        )
        self.assertEqual(
            {
                item.command_contract_identity
                for item in closure.record.component_authorities
            },
            {item.identity for item in contracts},
        )
        SchemaCatalog().validate(closure.record.SCHEMA, closure.record.to_dict())
        self.assertEqual(
            StandardToolchainClosure.from_dict(closure.record.to_dict()),
            closure.record,
        )
        closure.require_unchanged()

    def test_runtime_accepts_only_one_coherent_toolchain_authority_path(self) -> None:
        _snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        closure = _toolchain_closure(execution, contracts, tool_bindings)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser_driver = object()
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=root / "objects",
                toolchain_closure=closure,
                browser_driver=browser_driver,
            )
            self.assertIsInstance(
                runtime.application.lifecycle.candidate_repair_port,
                FilesystemStandardCandidateRepairAdapter,
            )
            self.assertIs(runtime.lifecycle_ports.browser_driver, browser_driver)
            readiness = runtime.production_readiness(
                PlannedStandardProject(_selection(), execution)
            )
            with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
                assemble_filesystem_standard_project_runtime(
                    generator=lambda _prepared: None,
                    object_root=root / "ambiguous",
                    contracts=contracts,
                    toolchain_closure=closure,
                )

        self.assertNotIn("toolchain-closure-unverified", readiness.blockers)
        self.assertNotIn("toolchain-closure-invalid", readiness.blockers)
        self.assertNotIn("toolchain-closure-plan-mismatch", readiness.blockers)

    def test_readiness_requires_complete_cache_publication_capability(self) -> None:
        _snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        closure = _toolchain_closure(execution, contracts, tool_bindings)

        with tempfile.TemporaryDirectory() as temporary:
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=Path(temporary) / "objects",
                toolchain_closure=closure,
                indexer=_Indexer(),
                source_cache_publisher=_CompleteCachePublisher(),
                source_cache_restorer=_CacheRestorer(),
                checkpoint_store=_CheckpointStore(),
            )
            readiness = runtime.production_readiness(
                PlannedStandardProject(_selection(), execution)
            )

        self.assertTrue(readiness.ready, readiness.blockers)
        self.assertEqual(readiness.blockers, ())

    def test_checkpoint_composition_closes_restart_readiness(self) -> None:
        _snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        closure = _toolchain_closure(execution, contracts, tool_bindings)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=root / "objects",
                toolchain_closure=closure,
                indexer=_Indexer(),
                source_cache_publisher=_CompleteCachePublisher(),
                source_cache_restorer=_CacheRestorer(),
            )
            context_recorder = runtime.context_evidence_recorder
            self.assertIn(
                "stage-checkpoint-round-trip-unavailable",
                runtime.production_readiness(
                    PlannedStandardProject(_selection(), execution)
                ).blockers,
            )
            composed = compose_filesystem_standard_lifecycle_checkpoints(
                runtime, checkpoint_root=root / "checkpoints"
            )
            readiness = composed.production_readiness(
                PlannedStandardProject(_selection(), execution)
            )

        self.assertTrue(readiness.ready, readiness.blockers)
        self.assertIs(
            composed.application.lifecycle.checkpoint_recorder,
            composed.checkpoint_store,
        )
        self.assertIs(composed.context_evidence_recorder, context_recorder)
        self.assertIs(
            composed.application.lifecycle.context_evidence_recorder,
            context_recorder,
        )

    def test_runtime_reports_plan_mismatch_and_observation_drift(self) -> None:
        _snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        closure = _toolchain_closure(execution, contracts, tool_bindings)
        foreign_plan = replace(
            execution,
            planner_identity=canonical_identity({"planner": "foreign"}),
        )

        with tempfile.TemporaryDirectory() as temporary:
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=Path(temporary) / "objects",
                toolchain_closure=closure,
            )
            mismatched = runtime.production_readiness(
                PlannedStandardProject(_selection(), foreign_plan)
            )
            closure.dependency_observation.components[0]["name"] = "drifted"
            drifted = runtime.production_readiness(
                PlannedStandardProject(_selection(), execution)
            )

        self.assertIn("toolchain-closure-plan-mismatch", mismatched.blockers)
        self.assertIn("toolchain-closure-invalid", drifted.blockers)

    def test_projection_rejects_missing_provider_or_dependency_custody(self) -> None:
        _snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        arguments = {
            "contracts": contracts,
            "tool_bindings": tool_bindings,
            "observer_identity": canonical_identity({"observer": "test@1"}),
        }
        with self.assertRaisesRegex(ValueError, "provider bindings must cover"):
            project_standard_toolchain_closure(
                execution,
                provider_environment={
                    contracts[0].artifact_export.export_id: (
                        "UNPLANNED_PROVIDER",
                        "artifact-manifest.json",
                    )
                },
                dependency_observation=HostDependencyObservation(
                    ({"bom-ref": "tool", "name": "tool"},), ()
                ),
                **arguments,
            )

        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            project_standard_toolchain_closure(
                execution,
                dependency_observation=HostDependencyObservation((), ()),
                **arguments,
            )

    def test_runtime_plan_preserves_explicit_authored_assets(self) -> None:
        source_snapshot, execution = _fixture()
        lock = source_snapshot.authority.lock
        asset = AuthoredBinaryAsset(
            lock.root_revision,
            "runtime-icon",
            "assets/runtime-icon.png",
            "runtime-asset",
            lock.target_profile_identity,
            BlobRef("a" * 64, 12),
            canonical_identity({"asset-authorization": "runtime-icon"}),
        )
        model_selector = mock.Mock()
        model_selector.identities.return_value = {
            plan.component_revision.uri: plan.generation_key.model_identity
            for plan in execution.generation_plans
        }
        resolution_plan_identity = canonical_identity({"synthetic-plan": "assets"})
        current_plan = mock.Mock(identity=resolution_plan_identity)
        planner = mock.Mock()
        planner.plan_snapshot.return_value = current_plan
        audit_identity = canonical_identity({"synthetic-audit": "assets"})
        audit_store = mock.Mock()
        audit_store.read.return_value = mock.Mock(identity=audit_identity)
        snapshot = LockedGenerationAuthoritySnapshot(
            source_snapshot.authority,
            mock.Mock(),
            mock.Mock(),
            planner,
            resolution_plan_identity,
            audit_store,
            audit_identity,
            "host",
            (),
        )
        contracts, tool_bindings = _command_contracts(execution)

        with tempfile.TemporaryDirectory() as temporary:
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=Path(temporary) / "objects",
                toolchain_closure=_toolchain_closure(
                    execution, contracts, tool_bindings
                ),
                planning=FilesystemStandardProjectPlanningAdapter(
                    coding_cli_selector=_selection,
                    model_selector=model_selector,
                ),
            )
            planned = runtime.plan(snapshot, assets=(asset,))

        root_plan = next(
            plan
            for plan in planned.execution_plan.generation_plans
            if plan.component_revision == lock.root_revision
        )
        self.assertEqual(root_plan.generation_key.asset_identities, (asset.identity,))
        model_selector.identities.assert_called_once_with(
            snapshot,
            coding_cli="codex",
        )

    def test_runtime_execute_owns_preparation_and_lifecycle_delegation(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        revisions = tuple(
            plan.component_revision for plan in execution.generation_plans
        )
        invalidation = ComponentInvalidationDecision(
            "runtime-facade",
            execution.root_revision,
            ComponentChangeSurface.LOCAL_AUTHORITY,
            revisions,
            revisions,
            revisions,
        )
        planned = PlannedStandardProject(_selection(), execution)
        expected_lifecycle = mock.sentinel.lifecycle

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=root / "objects",
                toolchain_closure=_toolchain_closure(
                    execution, contracts, tool_bindings
                ),
            )
            request = StandardProjectExecutionRequest(
                planned,
                root / "sources",
                invalidation,
                max_parallelism=3,
                budget=_budget(),
            )
            with mock.patch.object(
                type(runtime.application),
                "rebuild",
                autospec=True,
                return_value=expected_lifecycle,
            ) as rebuild:
                result = runtime.execute(snapshot, request)

        self.assertEqual(result.planned, planned)
        self.assertEqual(result.prepared.execution_plan, execution)
        self.assertIs(result.lifecycle, expected_lifecycle)
        self.assertEqual(
            result.local_build_cache_report,
            {"hits": 0, "misses": 0, "hit_seconds": 0.0, "build_seconds": 0.0},
        )
        rebuild.assert_called_once_with(
            runtime.application,
            result.prepared,
            component_lock=snapshot.authority.lock,
            invalidation=invalidation,
            source_cache_memberships=None,
            resume_candidates=None,
            max_parallelism=3,
        )

    def test_accepted_source_missing_key_fails_before_generator_fallback(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        revisions = tuple(
            plan.component_revision for plan in execution.generation_plans
        )
        invalidation = ComponentInvalidationDecision(
            "runtime-cache-only",
            execution.root_revision,
            ComponentChangeSurface.SOURCE_REPLACEMENT,
            (),
            revisions,
            revisions,
        )
        generator = mock.Mock(name="coding-cli-generator")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=generator,
                object_root=root / "objects",
                toolchain_closure=_toolchain_closure(
                    execution, contracts, tool_bindings
                ),
                source_cache_restorer=_CacheRestorer(),
            )
            request = StandardProjectExecutionRequest(
                PlannedStandardProject(_selection(), execution),
                root / "sources",
                invalidation,
                accepted_source_only=True,
                expected_framework_distribution_identity=canonical_identity(
                    {"framework": "test"}
                ),
                worker_source_selectors=StandardSourceSelectorSet(
                    StandardSourceSelectorScope.TARGET_SPECIFIC,
                    (StandardSourceSelector("target.profile", "windows"),),
                ),
                directory_custody_identity=canonical_identity(
                    {"directory-custody": "test"}
                ),
            )
            with self.assertRaises(StandardAcceptedSourceContinuationError) as raised:
                runtime.execute(snapshot, request)

        self.assertEqual(raised.exception.code, "source_cache.runtime_absent")
        generator.assert_not_called()

    def test_runtime_full_rebuild_persists_context_evidence_in_its_cas(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        revisions = tuple(
            plan.component_revision for plan in execution.generation_plans
        )
        invalidation = ComponentInvalidationDecision(
            "runtime-context-evidence",
            execution.root_revision,
            ComponentChangeSurface.LOCAL_AUTHORITY,
            revisions,
            revisions,
            revisions,
        )
        generated: list[str] = []

        def generator(prepared):
            generated.append(prepared.plan.component_revision.uri)
            raise ComponentSourceGenerationRunError(
                "private-source-generator-diagnostic"
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=generator,
                object_root=root / "objects",
                toolchain_closure=_toolchain_closure(
                    execution, contracts, tool_bindings
                ),
            )
            result = runtime.execute(
                snapshot,
                StandardProjectExecutionRequest(
                    PlannedStandardProject(_selection(), execution),
                    root / "sources",
                    invalidation,
                    max_parallelism=2,
                    budget=_budget(),
                ),
            )
            recorder = runtime.context_evidence_recorder
            self.assertIsNotNone(recorder)
            assert recorder is not None
            self.assertIs(
                runtime.application.lifecycle.context_evidence_recorder,
                recorder,
            )
            self.assertTrue(generated)
            self.assertEqual(
                result.lifecycle.context_prompt_journal_identities,
                recorder.prompt_journal_identities,
            )
            self.assertEqual(
                result.lifecycle.context_benchmark_records,
                recorder.benchmark_records,
            )
            self.assertEqual(
                result.lifecycle.context_cache_report,
                recorder.cache_report(),
            )
            self.assertEqual(len(recorder.records), len(execution.generation_plans))
            for recorded in recorder.records:
                self.assertEqual(
                    recorder.journal.get_prompt_journal(recorded.journal_reference),
                    recorded.journal,
                )
                self.assertEqual(
                    recorder.journal.get_benchmark_record(recorded.benchmark_reference),
                    recorded.benchmark,
                )
            stored = b"".join(
                recorder.journal.cas.get_bytes(reference)
                for reference in recorder.journal.cas.iter_refs()
            )

        self.assertNotIn(b"private-source-generator-diagnostic", stored)

    def test_runtime_rejects_foreign_publication_lock_before_execution(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        planned = PlannedStandardProject(_selection(), execution)
        revisions = tuple(
            plan.component_revision for plan in execution.generation_plans
        )
        invalidation = ComponentInvalidationDecision(
            "runtime-release",
            execution.root_revision,
            ComponentChangeSurface.LOCAL_AUTHORITY,
            revisions,
            revisions,
            revisions,
        )
        foreign_lock = replace(
            snapshot.authority.lock,
            resolver_identity=canonical_identity({"resolver": "foreign"}),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=root / "objects",
                toolchain_closure=_toolchain_closure(
                    execution, contracts, tool_bindings
                ),
            )
            publication_request = StandardProjectPublicationRequest(
                mock.Mock(),
                foreign_lock,
                (),
                mock.sentinel.source_bundle,
                mock.sentinel.evidence_manifest,
                mock.sentinel.read_blob,
                canonical_identity({"security": "test"}),
                SecurityProfile.CONSTRAINED,
                "release-actor",
                "must fail closed",
            )
            with (
                mock.patch.object(
                    type(runtime.application), "rebuild", autospec=True
                ) as rebuild,
                self.assertRaisesRegex(ValueError, "differs from locked snapshot"),
            ):
                runtime.execute_and_publish(
                    snapshot,
                    StandardProjectExecutionRequest(
                        planned,
                        root / "sources",
                        invalidation,
                        budget=_budget(),
                    ),
                    publication_request,
                )

        rebuild.assert_not_called()

    def test_factory_assembles_and_prepares_without_cli_private_helpers(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)

        def generator(_prepared):
            raise AssertionError("preparation must not invoke source generation")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=generator,
                object_root=root / "objects",
                toolchain_closure=_toolchain_closure(
                    execution, contracts, tool_bindings
                ),
            )
            readiness = runtime.production_readiness(
                PlannedStandardProject(_selection(), execution)
            )
            prepared = runtime.prepare(
                snapshot,
                PlannedStandardProject(_selection(), execution),
                source_root=root / "sources",
                budget=_budget(),
            )

        self.assertEqual(prepared.execution_plan, execution)
        self.assertEqual(len(prepared.nodes), len(execution.generation_plans))
        self.assertIs(runtime.lifecycle_ports.source_trees, runtime.source_trees)
        registered = runtime.application.lifecycle.generator
        self.assertIsInstance(registered, RegisteredSourceGenerationRunner)
        self.assertIs(registered.registry, runtime.source_trees)
        self.assertIs(runtime.application.lifecycle.builder, runtime.lifecycle_ports)
        self.assertIs(runtime.application.lifecycle.indexer, runtime.lifecycle_ports)
        self.assertIs(
            runtime.application.lifecycle.source_cache_publisher,
            runtime.lifecycle_ports,
        )
        self.assertFalse(readiness.ready)
        self.assertEqual(
            set(readiness.blockers),
            {
                "indexer-unconfigured",
                "source-cache-round-trip-unavailable",
                "stage-checkpoint-round-trip-unavailable",
            },
        )

    def test_preparation_rejects_a_symlinked_source_runtime_root(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=root / "objects",
                toolchain_closure=_toolchain_closure(
                    execution, contracts, tool_bindings
                ),
            )
            with (
                mock.patch.object(Path, "is_symlink", return_value=True),
                self.assertRaisesRegex(ValueError, "cannot be a symlink"),
            ):
                runtime.prepare(
                    snapshot,
                    PlannedStandardProject(_selection(), execution),
                    source_root=root / "alias",
                    budget=_budget(),
                )


if __name__ == "__main__":
    unittest.main()
