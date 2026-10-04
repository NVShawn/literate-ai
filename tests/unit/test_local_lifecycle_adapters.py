"""Executable integration of the provider-neutral lifecycle with local adapters."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.builders import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    discover_python_toolchain,
)
from literate_ai.adapters.intelligence import (
    SourceIntelligenceProviderSelection,
)
from literate_ai.adapters.lifecycle import (
    GeneratedTreeSecurityClassifier,
    LifecycleEventStoreAdapter,
    PipelineValidatorAdapter,
    PolicyBuildAuthorizer,
    PythonBuildAdapter,
    WorkspaceTreeAdapter,
)
from literate_ai.adapters.lifecycle import local as local_adapter_module
from literate_ai.application import (
    GeneratedTestSuitePolicy,
    GenerationOrchestrator,
    GenerationRequest,
    GenerationStatus,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CycloneDxManagedGraph,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    canonical_identity,
)
from literate_ai.ports import require_committed_tree_result
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequestDeclaration,
    LiveBuildAuthorizationVerifier,
    RuleBasedSourceScanner,
    SecurityPolicy,
    baseline_python_rules,
)
from literate_ai.storage import AppendOnlyEventStore
from literate_ai.validation import PythonSyntaxValidator, ValidationPipeline
from literate_ai.workspace import WorkspaceTreeStore
from tests.support.fixtures_test_application_generation import (
    TEST_RECIPE_IDENTITY,
    TEST_SPECIFICATION_REFERENCES,
    DependencyResolver,
    GeneratedTestRunner,
    IndependentAcceptanceRunner,
    Models,
    Readiness,
    execution_plan,
    generation_context,
    locked_authority_fixture,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def source_intelligence_selection(provider=None):
    del provider
    return SourceIntelligenceProviderSelection(
        SourceIntelligenceStage.SOURCE_GENERATION,
        SourceIntelligenceMode.OFF,
        "none",
        None,
    )


def dependency_resolution():
    authority = locked_authority_fixture()
    managed_graph = CycloneDxManagedGraph.from_component_lock(authority.lock)
    return DependencyResolver([], managed_graph).resolve(
        {
            "effective_revision_digest": canonical_identity({"revision": 1}).uri,
            "source_bundle_digest": canonical_identity({"source": 1}).uri,
            "files": {CYCLONEDX_SOURCE_SBOM_PATH: "{}"},
        },
        {
            "artifact_digest": canonical_identity({"artifact": 1}).uri,
            "source_bundle_digest": canonical_identity({"source": 1}).uri,
        },
    )


class LocalLifecycleAdapterTests(unittest.TestCase):
    def test_generated_file_paths_use_one_portable_posix_representation(self) -> None:
        unsafe = (
            "../escape.py",
            "source/../escape.py",
            "source\\..\\escape.py",
            "/absolute.py",
            "C:/escape.py",
            "C:escape.py",
            "//?/C:/escape.py",
            "source//main.py",
            "source/./main.py",
            "source/NUL.txt",
            "source/NUL .txt",
            "source/CONOUT$",
            "source/COM¹.txt",
            "source/file:stream.py",
            "source/trailing.",
        )

        for path in unsafe:
            with self.subTest(path=path):
                with self.assertRaises((TypeError, ValueError)):
                    local_adapter_module._files({"files": {path: "print('unsafe')\n"}})

        self.assertEqual(
            local_adapter_module._files(
                {"files": {"source/nested/main.py": "print('safe')\n"}}
            ),
            {"source/nested/main.py": b"print('safe')\n"},
        )

        for files in (
            {"source/A.py": "first\n", "source/a.py": "second\n"},
            {"source/a": "file\n", "source/a/main.py": "child\n"},
        ):
            with self.subTest(files=tuple(files)):
                with self.assertRaises(ValueError):
                    local_adapter_module._files({"files": files})

    @unittest.skipUnless(os.name == "posix", "symlink containment test is POSIX-only")
    def test_materialization_proves_containment_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            materialized = root / "materialized"
            outside = root / "outside"
            materialized.mkdir()
            outside.mkdir()
            (materialized / "source").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "escapes"):
                local_adapter_module._materialize_files(
                    materialized,
                    {"source/main.py": b"print('must not escape')\n"},
                )

            self.assertFalse((outside / "main.py").exists())

    def test_generated_tree_is_validated_classified_built_and_committed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = locked_authority_fixture()
            managed_graph = CycloneDxManagedGraph.from_component_lock(authority.lock)
            python_toolchain = discover_python_toolchain()
            plan = execution_plan(authority)
            request = GenerationRequest(
                locked_authority=authority,
                component_revision=authority.lock.root_revision,
                generation_context=generation_context(authority, plan),
                execution_plan=plan,
                build_request_declaration=BuildRequestDeclaration(
                    effective_revision_digest=authority.lock.root_revision.uri,
                    builder_id="builder:python-bytecode@1",
                    toolchain_digest=python_toolchain.identity,
                    sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
                    requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
                    allowed_outputs=("python-bytecode",),
                ),
                workspace_reference="generated/application",
                generated_test_suite_policy=GeneratedTestSuitePolicy(
                    TEST_RECIPE_IDENTITY,
                    TEST_SPECIFICATION_REFERENCES,
                ),
                managed_sbom_graph=managed_graph,
            )
            policy = SecurityPolicy(canonical_identity({"policy": "test"}).uri)
            calls: list[str] = []
            workspace = WorkspaceTreeAdapter(
                WorkspaceTreeStore(root / "workspace"),
                source_intelligence=source_intelligence_selection(),
            )
            event_store = LifecycleEventStoreAdapter(
                AppendOnlyEventStore(root / "events")
            )
            orchestrator = GenerationOrchestrator(
                readiness_provider=Readiness(),
                model_provider=Models(calls),
                validator=PipelineValidatorAdapter(
                    ValidationPipeline(
                        (PythonSyntaxValidator(),), required_categories=("syntax",)
                    )
                ),
                classifier=GeneratedTreeSecurityClassifier(
                    policy,
                    RuleBasedSourceScanner(
                        "scanner:baseline-python@1", baseline_python_rules()
                    ),
                    "generator:test@1",
                    "test-generation-root",
                ),
                build_authorizer=PolicyBuildAuthorizer(
                    policy,
                    "test-maintainer",
                    "acknowledged unsandboxed executable integration test",
                    yolo_acknowledged=True,
                    clock=lambda: NOW,
                ),
                builder=PythonBuildAdapter(
                    artifact_store=root / "artifacts",
                    toolchain=python_toolchain,
                    clock=lambda: NOW,
                    authorization_verifier=LiveBuildAuthorizationVerifier(
                        lambda: AuthorizationRevocationSet()
                    ),
                ),
                dependency_resolver=DependencyResolver(calls, managed_graph),
                generated_test_runner=GeneratedTestRunner(calls),
                acceptance_runner=IndependentAcceptanceRunner(calls),
                workspace=workspace,
                event_store=event_store,
            )

            run = orchestrator.execute(request)

            self.assertEqual(run.status, GenerationStatus.COMPLETE)
            accepted = WorkspaceTreeStore(root / "workspace").resolve(
                "generated/application"
            )
            self.assertIsNotNone(accepted)
            self.assertEqual((accepted / "source/main.py").read_text(), "print('ok')\n")
            self.assertTrue(tuple((root / "artifacts").glob("*/source/main.pyc")))
            self.assertEqual(
                len(event_store.stream(run.run_id)),
                len(run.events),
            )
            build_step = run.step("build")
            authorization_step = run.step("authorize-build")
            classification_step = run.step("classify")
            self.assertIsNotNone(build_step)
            self.assertIsNotNone(authorization_step)
            self.assertIsNotNone(classification_step)
            self.assertEqual(authorization_step.output["profile"], "yolo")
            expected_build_request = request.build_request_declaration.realize(
                build_step.output["source_bundle_digest"]
            )
            self.assertEqual(
                authorization_step.output["request_digest"],
                canonical_identity(expected_build_request.to_dict()).uri,
            )
            self.assertEqual(
                build_step.output["source_bundle_digest"],
                classification_step.output["source_digests"][0],
            )

    def test_source_generation_off_commits_without_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory) / "workspace")
            selection = SourceIntelligenceProviderSelection(
                SourceIntelligenceStage.SOURCE_GENERATION,
                SourceIntelligenceMode.OFF,
                "none",
                None,
            )
            adapter = WorkspaceTreeAdapter(
                store,
                source_intelligence=selection,
            )
            resolution = dependency_resolution()
            prepared = adapter.prepare({"main.py": b"value = 1\n"}, resolution)

            result = adapter.commit(prepared, "without/intelligence", resolution)

            self.assertIsNone(result["source_intelligence"])
            self.assertEqual(
                result["source_intelligence_status"],
                {
                    "schema": "literate-ai/source-intelligence-stage-status@1",
                    "stage": "source-generation",
                    "mode": "off",
                    "state": "off",
                    "provider_id": "none",
                },
            )
            accepted = store.resolve("without/intelligence")
            self.assertIsNotNone(accepted)
            self.assertFalse((accepted / ".codegraph").exists())
            normalized = require_committed_tree_result(
                result,
                tree_digest=prepared["tree_digest"],
                reference="without/intelligence",
                dependency_resolution=resolution,
            )
            self.assertIsNone(normalized["source_intelligence"])


if __name__ == "__main__":
    unittest.main()
