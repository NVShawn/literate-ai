"""Real SDK admission binds planning, node preparation and bounded context."""

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from literate_ai.adapters.generation_preparation import (
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_generation import native_sdk_generation_identities
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectPlanningAdapter,
    PlannedStandardProject,
    assemble_filesystem_standard_project_runtime,
)
from literate_ai.application.component_execution_planning import (
    ComponentExecutionPlanningError,
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    ComponentGenerationContextError,
    prepare_component_generation_context,
)
from literate_ai.application.generation_preparation import GenerationPreparationError
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.executable_components import (
    ComponentExecutionPlan,
    ContextAuthorityKind,
)
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_native_sdk_source_build as test_native_sdk_source_build
from tests.support.fixtures_test_component_node_generation_preparation import _budget
from tests.support.fixtures_test_native_sdk_closure import linked_recipe
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_standard_project_factory import (
    _command_contracts,
    _selection,
    _toolchain_closure,
)


def preparation_recipe(fixture):
    linked_recipe(fixture)
    path = fixture.component / "component.md"
    document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
    document["requires"].append(
        {
            **document["requires"][0],
            "requirement_id": "provider-api",
            "dependency_kind": "generation",
        }
    )
    path.write_bytes(render_authoring_markdown(document, body))
    for component in (fixture.component, fixture.component / "provider"):
        path = component / "component.md"
        document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
        if component != fixture.component:
            document["kind"] = "library"
        document["authoring_inputs"].extend(
            {
                "kind": "specification-to-source-skill",
                "uri": f"skills/specification-to-source/{name}/SKILL.md",
            }
            for name in (
                "portable-specification-planning",
                "portable-application-implementation",
            )
        )
        path.write_bytes(render_authoring_markdown(document, body))
    (fixture.component.parent / "skills/implement.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
                "skill_id": "fixture-implementation",
                "version": "1.0.0",
                "title": "Fixture implementation",
                "stages": ["generate"],
                "dependencies": [],
                "instructions": "Implement the declared public contracts.",
                "limitations": ["Do not invent behavior."],
                "trust": "fixture-reviewed",
            }
        )
    )


class NativeSdkPreparationTests(unittest.TestCase):
    def test_admitted_sdk_keys_and_bounded_context_preserve_component_ownership(self):
        fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.configure_recipe_fixture = preparation_recipe
        fixture.setUp()
        service = fixture.service()
        service.build()
        snapshot = fixture.snapshot
        lock = snapshot.authority.lock
        inputs = NativeSdkConsumerInputs(snapshot=snapshot, services=(service,))
        ids = native_sdk_generation_identities(inputs, snapshot)
        owner = fixture.selection.component_revision
        models = {
            node.revision.identity.uri: canonical_identity(
                {"model": node.revision.identity.uri}
            )
            for node in lock.nodes
        }
        with self.assertRaisesRegex(
            ComponentExecutionPlanningError, "SDK build admission"
        ):
            plan_component_execution(lock, model_identities=models)
        for bad in (
            {},
            {lock.root_revision.uri: ids[owner.uri]},
            {owner.uri: ()},
            {owner.uri: list(ids[owner.uri])},
            {owner.uri: ("untyped-identity",)},
        ):
            with self.assertRaises(ComponentExecutionPlanningError):
                plan_component_execution(
                    lock, model_identities=models, native_sdk_input_identities=bad
                )
        execution = plan_component_execution(
            lock, model_identities=models, native_sdk_input_identities=ids
        )
        self.assertEqual(
            ComponentExecutionPlan.from_dict(execution.to_dict()), execution
        )
        SchemaCatalog().validate(execution.SCHEMA, execution.to_dict())
        plans = {plan.component_revision: plan for plan in execution.generation_plans}
        self.assertEqual(
            plans[owner].generation_key.native_sdk_input_identities, ids[owner.uri]
        )
        self.assertEqual(
            plans[lock.root_revision].generation_key.native_sdk_input_identities, ()
        )
        self.assertNotIn(
            "native_sdk_input_identities",
            plans[lock.root_revision].generation_key.to_dict(),
        )
        changed = plan_component_execution(
            lock,
            model_identities=models,
            native_sdk_input_identities={
                owner.uri: (canonical_identity("different admitted SDK"),)
            },
        )
        changed_plans = {
            plan.component_revision: plan for plan in changed.generation_plans
        }
        self.assertNotEqual(
            changed_plans[owner].generation_key.identity,
            plans[owner].generation_key.identity,
        )
        self.assertEqual(
            changed_plans[lock.root_revision].generation_key.identity,
            plans[lock.root_revision].generation_key.identity,
        )
        adapter = LockedComponentNodePreparationAdapter(native_sdk_inputs=inputs)
        with self.assertRaises(GenerationPreparationError):
            LockedComponentNodePreparationAdapter().project(snapshot, plans[owner])
        with self.assertRaises(GenerationPreparationError):
            adapter.project(snapshot, changed_plans[owner])
        with self.assertRaises(TypeError):
            LockedComponentNodePreparationAdapter(native_sdk_inputs=ids).project(
                snapshot, plans[owner]
            )
        with self.assertRaisesRegex(GenerationPreparationError, "live admitted SDK"):
            adapter.project(
                snapshot,
                replace(
                    plans[owner],
                    generation_key=replace(
                        plans[owner].generation_key, native_sdk_input_identities=()
                    ),
                ),
            )
        projections = {
            revision: adapter.project(snapshot, plan)
            for revision, plan in plans.items()
        }
        self.assertEqual(len(projections[owner].recipe.native_sdk_dependencies), 1)
        self.assertEqual(
            projections[lock.root_revision].recipe.native_sdk_dependencies, ()
        )
        self.assertEqual(
            len(projections[lock.root_revision].recipe.library_dependencies), 1
        )
        self.assertEqual(
            projections[lock.root_revision]
            .recipe.library_dependencies[0]
            .provider_component_revision,
            owner,
        )
        for revision, projection in projections.items():
            native_segments = tuple(
                segment
                for segment in projection.authority_segments
                if segment.authority_kind
                is ContextAuthorityKind.LOCAL_NATIVE_SDK_METADATA
            )
            self.assertEqual(
                tuple(segment.content_identity for segment in native_segments),
                ids.get(revision.uri, ()),
            )
            envelope = projection.recipe.prompt(
                include_locked_authority_documents=False
            ).encode()
            prepared = prepare_component_generation_context(
                plans[revision],
                framework_envelope=envelope,
                authority_segments=projection.authority_segments,
                budget=_budget(),
            )
            SchemaCatalog().validate(
                prepared.request.SCHEMA, prepared.request.to_dict()
            )
            if revision == owner:
                self.assertIn(b'"package":"vendor_math"', prepared.prompt)
                with self.assertRaises(ComponentGenerationContextError):
                    prepare_component_generation_context(
                        plans[revision],
                        framework_envelope=envelope,
                        authority_segments=tuple(
                            segment
                            for segment in projection.authority_segments
                            if segment.authority_kind
                            is not ContextAuthorityKind.LOCAL_NATIVE_SDK_METADATA
                        ),
                        budget=_budget(),
                    )
                bad_segment = replace(
                    native_segments[0], source_component_revision=lock.root_revision
                )
                with self.assertRaises(ComponentGenerationContextError):
                    prepare_component_generation_context(
                        plans[revision],
                        framework_envelope=envelope,
                        authority_segments=tuple(
                            bad_segment if segment is native_segments[0] else segment
                            for segment in projection.authority_segments
                        ),
                        budget=_budget(),
                    )
            else:
                self.assertNotIn(b'"package":"vendor_math"', prepared.prompt)
        model_selector = SimpleNamespace(identities=lambda *args, **kwargs: models)
        planned = FilesystemStandardProjectPlanningAdapter(
            model_selector=model_selector, native_sdk_inputs=inputs
        ).plan_for_coding_cli(snapshot, coding_cli="opencode")
        self.assertEqual(planned, execution)
        contracts, tool_bindings = _command_contracts(execution)
        component = fixture.recipe_fixture.component
        runtime = assemble_filesystem_standard_project_runtime(
            generator=lambda _: self.fail("preparation must not invoke generation"),
            object_root=component.parent / "prepared-objects",
            toolchain_closure=_toolchain_closure(execution, contracts, tool_bindings),
            native_sdk_inputs=inputs,
        )
        prepared_project = runtime.prepare(
            snapshot,
            PlannedStandardProject(replace(_selection(), name="opencode"), execution),
            source_root=component.parent / "prepared-source",
            budget=_budget(),
        )
        self.assertEqual(len(prepared_project.nodes), 2)
        path = component / "provider" / "integration.md"
        path.write_bytes(path.read_bytes() + b"\nChanged integration contract.\n")
        with self.assertRaises(LockedGenerationAuthorityReaderError):
            runtime.node_preparation.guard(snapshot)
