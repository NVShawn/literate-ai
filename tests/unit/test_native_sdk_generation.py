"""Real admitted SDK projection into content-bound generation recipes."""

import unittest
from dataclasses import replace

from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.models import RecipeNativeSdkDependency
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_generation import bind_native_sdk_generation_inputs
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.sbom import project_component_lock_managed_graph
from tests.unit import test_native_sdk_source_build
from tests.unit.test_coding_cli_generation import flavor, recipe
from tests.unit.test_native_sdk_closure import linked_recipe


class NativeSdkGenerationTests(unittest.TestCase):
    def test_real_sdk_projection_binds_exact_imports_and_public_contract(self):
        fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.configure_recipe_fixture = linked_recipe
        fixture.setUp()
        service = fixture.service()
        built = service.build()
        inputs = NativeSdkConsumerInputs(snapshot=fixture.snapshot, services=(service,))
        lock = fixture.snapshot.authority.lock
        revision = built.selection.component_revision
        base = replace(
            recipe(flavor("python")),
            component_lock_identity=lock.identity,
            managed_sbom_graph=project_component_lock_managed_graph(lock, revision),
        )
        selected = bind_native_sdk_generation_inputs(base, inputs, revision)
        self.assertNotEqual(base.identity, selected.identity)
        self.assertEqual(
            base.identity, replace(base, native_sdk_dependencies=()).identity
        )
        self.assertEqual(
            bind_native_sdk_generation_inputs(selected, inputs, revision), selected
        )
        (dependency,) = selected.native_sdk_dependencies
        self.assertIsInstance(dependency, RecipeNativeSdkDependency)
        self.assertEqual(
            dependency.input_identity,
            inputs.for_consumer(
                revision, target_identity=built.selection.target_identity
            )[0].identity,
        )
        self.assertEqual(dependency.consumer_revision, revision)
        self.assertEqual(
            dependency.sdk_snapshot_identity, built.product.snapshot.identity
        )
        self.assertEqual(
            dependency.source_build_plan_identity,
            built.product.snapshot.recipe_identity,
        )
        self.assertEqual(
            dependency.source_lock_identity, built.selection.source_lock.identity
        )
        self.assertEqual(
            dependency.integration_content,
            (fixture.recipe_fixture.component / "provider/integration.md").read_text(),
        )
        for include_documents in (True, False):
            prompt = selected.prompt(
                include_locked_authority_documents=include_documents
            )
            self.assertIn('"package":"vendor_math"', prompt)
            self.assertIn(
                dependency.integration_contract.identity.uri.split(":")[1], prompt
            )
            self.assertIn(dependency.integration_content.strip(), prompt)
            self.assertNotIn(str(fixture.fixture.source), prompt)
            self.assertNotIn(str(service.store.root), prompt)
            self.assertNotIn("#include", prompt)
        with self.assertRaisesRegex(TypeError, "live admitted"):
            bind_native_sdk_generation_inputs(base, dependency.to_dict(), revision)
        with self.assertRaisesRegex(ValueError, "locked consumer graph"):
            bind_native_sdk_generation_inputs(
                replace(
                    base,
                    component_lock_identity=canonical_identity("foreign lock"),
                    managed_sbom_graph=None,
                ),
                inputs,
                revision,
            )
        with self.assertRaisesRegex(ValueError, "different admitted inputs"):
            bind_native_sdk_generation_inputs(
                replace(
                    selected,
                    native_sdk_dependencies=(
                        replace(
                            dependency,
                            input_identity=canonical_identity("foreign input"),
                        ),
                    ),
                ),
                inputs,
                revision,
            )
        for name in (
            "input_identity",
            "selection_identity",
            "source_lock_identity",
            "source_build_plan_identity",
            "sdk_snapshot_identity",
            "target_identity",
        ):
            changed = replace(
                selected,
                native_sdk_dependencies=(
                    replace(
                        dependency, **{name: canonical_identity("different " + name)}
                    ),
                ),
            )
            self.assertNotEqual(changed.identity, selected.identity)
        with self.assertRaisesRegex(ValueError, "public contract"):
            replace(
                dependency,
                integration_content=dependency.integration_content + "changed",
            )
        with self.assertRaisesRegex(ValueError, "canonical and unique"):
            replace(base, native_sdk_dependencies=(dependency, dependency))
        with self.assertRaisesRegex(ValueError, "namespaces conflict"):
            replace(
                base,
                native_sdk_dependencies=(
                    dependency,
                    replace(
                        dependency, dependency_id=dependency.dependency_id + "-second"
                    ),
                ),
            )
        application = replace(
            base,
            managed_sbom_graph=project_component_lock_managed_graph(
                lock, lock.root_revision
            ),
        )
        projected_application = bind_native_sdk_generation_inputs(
            application, inputs, lock.root_revision
        )
        self.assertEqual(projected_application, application)
        self.assertEqual(projected_application.native_sdk_dependencies, ())
        self.assertNotIn("vendor_math", projected_application.prompt())
        with self.assertRaisesRegex(ValueError, "locked consumer graph"):
            bind_native_sdk_generation_inputs(base, inputs, lock.root_revision)
        path = fixture.recipe_fixture.component / "provider/integration.md"
        path.write_text(path.read_text() + "Changed after SDK admission.\n")
        with self.assertRaises(LockedGenerationAuthorityReaderError) as raised:
            bind_native_sdk_generation_inputs(base, inputs, revision)
        self.assertEqual(raised.exception.code, "component_lock.stale")
