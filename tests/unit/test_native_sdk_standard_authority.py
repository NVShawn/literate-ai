"""Real SDK producer custody binds Standard intent and composite build authority.

Per-node generation inputs are fixtures: public planning remains closed until the
consumer execution path is complete. SDK source capture, build and CAS are real.
"""

import json
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.dependencies import (
    build_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from literate_ai.adapters.dependencies.observation import PortableHostDependencyObserver
from literate_ai.adapters.lifecycle import (
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    local_generated_source_tree_identity,
)
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_package import NativeSdkPackageResources
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    realize_manifest,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
)
from literate_ai.contracts import CYCLONEDX_SOURCE_SBOM_PATH, CycloneDxLifecycle
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.sbom import project_component_lock_managed_graph
from literate_ai.storage.cas import BlobIntegrityError
from tests.support import (
    fixtures_test_native_sdk_source_build as test_native_sdk_source_build,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_standard_local_command_adapter import (
    _python_copy_lifecycle,
)


class NativeSdkStandardAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests(
            "test_real_policy_build_cache_and_relocated_native_execution"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.entrypoint_kind = getattr(self, "entrypoint_kind", None)
        self.fixture.configure_recipe_fixture = getattr(
            self, "configure_recipe_fixture", None
        )
        self.fixture.setUp()
        self.service = self.fixture.service()
        self.built = self.service.build()
        self.inputs = NativeSdkConsumerInputs(
            snapshot=self.fixture.snapshot, services=(self.service,)
        )
        root = self.fixture.fixture.root / "standard"
        root.mkdir()
        baseline, _, candidate, _ = _python_copy_lifecycle(root)
        original = next(iter(baseline.contracts.values()))
        self.contract = replace(
            original,
            component_revision=self.built.selection.component_revision,
            artifact_export=replace(
                original.artifact_export,
                target_identity=self.built.selection.target_identity,
            ),
        )
        self.arguments = dict(
            source_trees=baseline.source_trees,
            object_root=root / "sdk-objects",
            contracts=(self.contract,),
            tool_bindings=tuple(baseline.tool_bindings.values()),
        )
        self.ports = LocalStandardLifecyclePorts(
            **self.arguments, native_sdk_inputs=self.inputs
        )
        self.execution = SimpleNamespace(
            component_lock_identity=self.fixture.snapshot.authority.lock.identity
        )
        self.generation = SimpleNamespace(
            identity=candidate.component_generation_plan_identity,
            component_revision=self.contract.component_revision,
            direct_generation_edges=(),
        )
        self.candidate = replace(
            candidate, component_revision=self.contract.component_revision
        )

    def intent(self, ports=None):
        return (ports or self.ports).create(
            self.execution,
            self.generation,
            self.candidate,
            getattr(self, "provider_artifacts", ()),
            (),
        )

    def prepare_standard_build(self, *, extra_source_files=None):
        # Generation remains a fixture; source custody, SDK production and the
        # Standard resolved-BOM writer are real adapters with exact byte checks.
        registry = self.ports.source_trees
        original = registry.evidence(self.candidate.tree_identity)
        source = registry.resolve(self.candidate.tree_identity)
        for name, text in (extra_source_files or {}).items():
            (source / name).write_text(text)
        managed = project_component_lock_managed_graph(
            self.fixture.snapshot.authority.lock, self.contract.component_revision
        )
        content, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        (source / CYCLONEDX_SOURCE_SBOM_PATH).write_bytes(content)
        suite = json.loads(original.generated_test_suite_content)
        recipe = SimpleNamespace(
            identity=suite["recipe_identity"],
            managed_sbom_graph=managed,
            non_acceptance_document_paths=tuple(
                sorted(
                    {
                        ref
                        for case in suite["cases"]
                        for ref in case["specification_refs"]
                    }
                )
            ),
            all_documents=(),
        )
        self.candidate = replace(
            self.candidate,
            source_bom_identity=binding.bom_identity,
            tree_identity=local_generated_source_tree_identity(source),
        )
        registry.register(self.candidate, source, recipe=recipe)
        intent = self.intent()
        index = self.ports.index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        plan = self.ports.finalize(intent, self.ports.authorize(intent, index))
        return plan, content, managed

    def prepare_standard_package(self, program="fixture app", *, package=True):
        plan, content, managed = self.prepare_standard_build()
        artifact = self.ports.object_root / (
            "fixture-" + self.contract.component_revision.digest[:12]
        )
        existing_outputs = set(self.ports.object_root.iterdir())
        artifact.mkdir()
        (artifact / "app.py").write_text("import vendor_math\n")
        self.ports.dependency_observation = PortableHostDependencyObserver(
            toolchain_commands=((sys.executable,),)
        ).observe({"artifact_path": str(artifact)}, root_ref="fixture-host-root")
        resolved = self.ports._resolve_and_write_sbom(plan, artifact)
        boms = tuple(artifact.rglob("*.json"))
        self.assertEqual(len(boms), 1)
        document = json.loads(boms[0].read_bytes())
        sdk_components = [
            item
            for item in document["components"]
            if item["bom-ref"].startswith("urn:literate-ai:native-sdk:")
        ]
        self.assertEqual(len(sdk_components), 1)
        self.assertIn(
            {
                "name": "literate-ai:native-sdk-input",
                "value": self.inputs.for_scope(self.contract.component_revision)[
                    0
                ].identity.uri,
            },
            sdk_components[0]["properties"],
        )
        _, checked = validate_resolved_cyclonedx_bom(
            boms[0].read_bytes(),
            source_content=content,
            source_managed_graph=managed,
        )
        self.assertEqual(resolved, checked)
        self.assertEqual(
            set(self.ports.object_root.iterdir()), existing_outputs | {artifact}
        )
        # The consumer artifact and build observation below are explicit fixtures.
        # Exercise actual Standard export custody and packaging, without claiming
        # generated consumer execution or bypassing the public SDK build guard.
        export_path = artifact / self.contract.artifact_export.export_id
        if isinstance(program, dict):
            export_path.mkdir()
            for name, content in program.items():
                (export_path / name).write_text(content)
        else:
            export_path.write_text(program)
        (artifact / "artifact-manifest.json").write_text(
            json.dumps({"build_observation": canonical_identity("fixture build").uri})
        )
        output = self.ports._build_output(plan, artifact)
        manifest = realize_manifest(plan.manifest, output.exports)
        if not package:
            return plan, artifact, output, manifest
        graph = create_artifact_build_graph(
            build_system_driver_identity=manifest.build_system_driver_identity,
            manifests=(manifest,),
            link_roots=(output.exports[0].identity,),
        )
        self.package_graph = graph
        lock = self.fixture.snapshot.authority.lock
        package_plan, package_result = self.ports.create_project_package(
            lock, self.execution, SimpleNamespace(), graph, graph.link_plans[0]
        )
        custody = self.ports.project_package_custody(package_plan, package_result)
        resources = NativeSdkPackageResources(
            self.inputs,
            component_lock_identity=lock.identity,
            root_revision=lock.root_revision,
            target_identity=self.built.selection.target_identity,
        )
        self.assertTrue(resources.inputs)
        self.assertTrue(set(resources.inputs).issubset(package_plan.inputs))
        self.assertTrue(
            set(resources.runtime_requirements).issubset(
                package_plan.runtime_requirements
            )
        )
        self.assertFalse(package_result.standalone)
        self.assertEqual(
            [item.supplied_by_package for item in resources.runtime_requirements],
            [True, False],
        )
        resources.verify_materialized(custody.root)
        return plan, artifact, package_plan, package_result, custody, resources

    def test_standard_bom_writer_consumes_real_sdk_dependency_evidence(self):
        plan, artifact, package_plan, package_result, custody, resources = (
            self.prepare_standard_package()
        )
        (custody.root / resources.inputs[0].path).write_bytes(b"changed")
        with self.assertRaisesRegex(LocalStandardLifecycleError, "custody changed"):
            self.ports.project_package_custody(package_plan, package_result)
        empty = replace(plan.materialization, native_sdk_input_identities=())
        changed = replace(
            plan,
            materialization=empty,
            request=replace(plan.request, materialization_plan_identity=empty.identity),
        )
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "exact live native SDK"
        ):
            self.ports._resolve_and_write_sbom(changed, artifact)

    def test_exact_sdk_inputs_bind_request_intent_materialization_and_grant(self):
        intent = self.intent()
        (binding,) = self.inputs.for_consumer(
            self.contract.component_revision,
            target_identity=self.contract.artifact_export.target_identity,
        )
        self.assertEqual(intent.native_sdk_input_identities, (binding.identity,))
        baseline_ports = LocalStandardLifecyclePorts(**self.arguments)
        baseline = self.intent(baseline_ports)
        self.assertNotEqual(intent.identity, baseline.identity)
        self.assertNotEqual(
            intent.build_request_identity, baseline.build_request_identity
        )
        index = canonical_identity("current generated-source index")
        authorization = self.ports.authorize(intent, index)
        baseline_authorization = baseline_ports.authorize(baseline, index)
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "authorization differs"
        ):
            self.ports.finalize(intent, baseline_authorization)
        plan = self.ports.finalize(intent, authorization)
        baseline_plan = baseline_ports.finalize(baseline, baseline_authorization)
        self.assertEqual(
            plan.materialization.native_sdk_input_identities, (binding.identity,)
        )
        self.assertNotEqual(plan.request.identity, baseline_plan.request.identity)
        self.assertEqual(plan.provider_artifact_identities, ())
        self.assertEqual(plan.manifest.actions[0].dependency_artifacts, ())
        schemas = SchemaCatalog()
        for value, kind in (
            (intent, StandardComponentBuildIntent),
            (plan, StandardComponentBuildPlan),
        ):
            wire = value.to_dict()
            schemas.validate(wire["schema"], wire)
            self.assertEqual(kind.from_dict(wire), value)
        for changed in (
            replace(intent, native_sdk_input_identities=()),
            replace(
                intent, native_sdk_input_identities=(canonical_identity("foreign SDK"),)
            ),
            replace(intent, build_request=baseline.build_request),
        ):
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "exact live native SDK"
            ):
                self.ports.authorize(changed, index)
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "exact live native SDK"
            ):
                self.ports.finalize(changed, authorization)
        with self.assertRaisesRegex(TypeError, "live producer-backed"):
            LocalStandardLifecyclePorts(**self.arguments, native_sdk_inputs=(binding,))
        self.execution.component_lock_identity = canonical_identity("foreign lock")
        with self.assertRaisesRegex(LocalStandardLifecycleError, "Component lock"):
            self.intent()
        # A backend cannot dispatch a plan that lacks retained finalization.
        self.ports._plans_by_revision.pop(plan.component_revision.uri)
        with patch.object(self.ports, "_build_locked") as run:
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "exact finalized build plan"
            ):
                self.ports.build(plan, ())
            run.assert_not_called()
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "exact live native SDK"
        ):
            baseline_ports.build(plan, ())

    def test_corrupted_sdk_custody_refuses_after_intent_and_after_authorization(self):
        intent = self.intent()
        index = canonical_identity("current generated-source index")
        authorization = self.ports.authorize(intent, index)
        native = next(
            item
            for item in self.built.product.snapshot.files
            if item.path == self.built.product.snapshot.native_libraries[0]
        )
        path = self.service.store.path_for(native.blob)
        original = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(original + b"corruption")
        with self.assertRaises(BlobIntegrityError):
            self.intent()
        with self.assertRaises(BlobIntegrityError):
            self.ports.authorize(intent, index)
        with self.assertRaises(BlobIntegrityError):
            self.ports.finalize(intent, authorization)
