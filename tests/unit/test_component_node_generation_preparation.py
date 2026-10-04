"""Exact per-node locked generation preparation tests."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.models import RecipeDocument
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_preparation import (
    prepare_component_generation_nodes,
)
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.contracts import (
    CapabilityRequirement,
    ComponentContentSelector,
    ComponentInterfaceBinding,
    ComponentLock,
    ComponentLockNode,
    ContentIdentity,
    ContentReference,
    DependencyKind,
    ExecutableComponentEdge,
    FlavorAxis,
    FlavorCoordinate,
    FlavorDefinition,
    FlavorRevision,
    GenerationComplexityBudget,
    ModelScopeDecision,
    NodeFlavorSlotResolution,
    NodeTargetFlavorSelection,
    RequirementConstraintSatisfaction,
    SelectedNodeFlavor,
)
from tests.support.fixtures_test_component_lock_contracts import (
    component_authoring,
    identity,
    locked_revision,
)

ROOT = Path(__file__).resolve().parents[2]


def _raw_identity(content: bytes) -> ContentIdentity:
    return ContentIdentity.parse_uri(f"sha256:{hashlib.sha256(content).hexdigest()}")


def _reference(kind: str, uri: str, content: bytes) -> ContentReference:
    return ContentReference(kind, uri, _raw_identity(content))


def _budget() -> GenerationComplexityBudget:
    return GenerationComplexityBudget(
        max_prompt_bytes=1_000_000,
        max_estimated_tokens=250_000,
        max_document_count=100,
        max_direct_interface_bytes=100_000,
        max_dependency_fan_in=10,
        max_model_attempts=3,
        max_wall_time_ms=600_000,
        max_model_tokens=100_000,
        max_cost_microunits=50_000_000,
    )


class _Snapshot:
    def __init__(self, authority, contents, flavor_contents) -> None:
        self.authority = authority
        self._contents = contents
        self._flavor_contents = flavor_contents
        self.guard_count = 0

    def component_content(self, _authoring_identity, reference):
        return self._contents[reference.identity.uri]

    def flavor_content(self, _flavor_identity, reference):
        return self._flavor_contents[reference.identity.uri]

    def require_unchanged(self) -> None:
        self.guard_count += 1


def _fixture(
    *,
    component_models: dict[str, str] | None = None,
    flavor_model: str | None = None,
    skill_model: str | None = None,
    library_names: frozenset[str] = frozenset(),
    dependency_kind: DependencyKind = DependencyKind.GENERATION,
):
    component_models = component_models or {}
    implementation_skill_content = (
        ROOT
        / "skills/specification-to-source"
        / "portable-application-implementation/SKILL.md"
    ).read_bytes()
    if skill_model is not None:
        implementation_skill_content = implementation_skill_content.replace(
            b'trust: "repository-reviewed"\n',
            (
                f'models:\n  codex: "{skill_model}"\ntrust: "repository-reviewed"\n'
            ).encode(),
            1,
        )
    planning_skill_content = (
        ROOT / "skills/specification-to-source/portable-specification-planning/SKILL.md"
    ).read_bytes()
    skills = (
        _reference(
            "specification-to-source-skill",
            "skills/implement.md",
            implementation_skill_content,
        ),
        _reference(
            "specification-to-source-skill",
            "skills/plan.md",
            planning_skill_content,
        ),
    )
    flavor_spec = b"Generate a portable Python command-line application.\n"
    flavor_reference = _reference("specification", "spec.md", flavor_spec)
    flavor_definition = FlavorDefinition(
        FlavorCoordinate("tests", "python"),
        "1.0.0",
        "Python",
        FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
        (),
        (),
        (),
        (),
        (flavor_reference,),
        (),
        (),
        (),
        (),
        (),
        (),
        ("python",),
    )
    flavor_model_content = None
    flavor_model_reference = None
    if flavor_model is not None:
        flavor_model_content = (
            '{"schema":"literate-ai/coding-model-selection@1",'
            f'"models":{{"codex":"{flavor_model}"}}}}'
        ).encode()
        flavor_model_reference = _reference(
            "model-selection", "models/coding.json", flavor_model_content
        )
        flavor_definition = replace(
            flavor_definition,
            authoring_inputs=(flavor_model_reference,),
        )
    flavor = FlavorRevision(flavor_definition, None, ())
    names = ("application", "service", "storage")
    requirements = {
        "application": (
            CapabilityRequirement("service", "service-api", ">=1,<2", dependency_kind),
        ),
        "service": (
            CapabilityRequirement("storage", "storage-api", ">=1,<2", dependency_kind),
        ),
        "storage": (),
    }
    interfaces = {
        "application": None,
        "service": ("service-api", b'{"interface":"service-public"}'),
        "storage": ("storage-api", b'{"interface":"storage-public"}'),
    }
    authorings = {}
    for name in names:
        authoring = component_authoring(
            name,
            interface=(
                None if interfaces[name] is None else (interfaces[name][0], "unused")
            ),
            requirements=requirements[name],
        )
        if name in library_names:
            authoring = replace(authoring, kind="library", entrypoints=())
        if name in component_models:
            authoring = replace(
                authoring,
                authoring_inputs=tuple(
                    sorted(
                        (
                            *authoring.authoring_inputs,
                            ComponentContentSelector(
                                "model-selection", "models/coding.json"
                            ),
                        ),
                        key=lambda item: (item.kind, item.uri),
                    )
                ),
            )
        authorings[name] = authoring
    authorings["application"] = replace(
        authorings["application"],
        specification_roots=("component.md", "errors.md"),
    )
    contents: dict[str, bytes] = {}
    nodes = {}
    for name in names:
        component_model_content = None
        component_model_reference = None
        if name in component_models:
            component_model_content = (
                '{"schema":"literate-ai/coding-model-selection@1",'
                f'"models":{{"codex":"{component_models[name]}"}}}}'
            ).encode()
            component_model_reference = _reference(
                "model-selection", "models/coding.json", component_model_content
            )
        specification = f"Only implement the {name} Component.\n".encode()
        specification_inputs = [
            (
                "component.md" if name == "application" else f"specs/{name}.md",
                specification,
            )
        ]
        if name == "application":
            specification_inputs.append(
                ("errors.md", b"Return structured errors for invalid input.\n")
            )
        acceptance = (
            '{"invocations":[{"arguments":[{"component":"'
            + name
            + '"}]}],"result_shape":{"component":"string"}}'
        ).encode()
        acceptance_reference = _reference(
            "acceptance-contract", "acceptance/execution.json", acceptance
        )
        workflow = f'{{"workflow":"{name}"}}'.encode()
        routing = f'{{"routing":"{name}"}}'.encode()
        revision = locked_revision(
            name,
            selected_flavor="python",
            interface=(
                None if interfaces[name] is None else (interfaces[name][0], "unused")
            ),
            authoring=authorings[name],
        )
        public_interfaces = ()
        if interfaces[name] is not None:
            public_interfaces = (
                _reference(
                    "public-interface-contract",
                    f"interfaces/{interfaces[name][0]}.json",
                    interfaces[name][1],
                ),
            )
        revision = replace(
            revision,
            specifications=tuple(
                _reference("specification", uri, content)
                for uri, content in specification_inputs
            ),
            selected_flavor_revisions=(flavor.identity,),
            authoring_inputs=tuple(
                sorted(
                    (
                        *skills,
                        *(
                            (component_model_reference,)
                            if component_model_reference
                            else ()
                        ),
                    ),
                    key=lambda item: (item.kind, item.uri),
                )
            ),
            workflow_definition=_reference("workflow", "workflows/host.json", workflow),
            routing_policy=_reference(
                "routing-policy", "routing/default.json", routing
            ),
            acceptance_contracts=(acceptance_reference,),
            public_interfaces=public_interfaces,
        )
        from literate_ai.contracts.component_locking import (
            ordered_specification_set_identity,
        )

        revision = replace(
            revision,
            specification_set_identity=ordered_specification_set_identity(
                authorings[name].specification_provider, revision.specifications
            ),
        )
        selection = NodeTargetFlavorSelection(
            revision.identity,
            "host",
            identity("invoice-cli-target"),
            identity("selection-policy"),
            (
                NodeFlavorSlotResolution(
                    authorings[name].flavor_slots[0],
                    (SelectedNodeFlavor("python", flavor.identity),),
                ),
            ),
        )
        bindings = (
            ()
            if interfaces[name] is None
            else (
                ComponentInterfaceBinding(
                    revision.identity,
                    interfaces[name][0],
                    public_interfaces[0].identity,
                ),
            )
        )
        selected_node = ComponentLockNode(revision, selection, bindings)
        nodes[name] = selected_node
        for reference, content in (
            *zip(
                revision.specifications,
                (item[1] for item in specification_inputs),
                strict=True,
            ),
            (skills[0], implementation_skill_content),
            (skills[1], planning_skill_content),
            *(
                ((component_model_reference, component_model_content),)
                if component_model_reference is not None
                and component_model_content is not None
                else ()
            ),
            (revision.workflow_definition, workflow),
            (revision.routing_policy, routing),
            (acceptance_reference, acceptance),
            *((reference, interfaces[name][1]) for reference in public_interfaces),
        ):
            contents[reference.identity.uri] = content

    edges = []
    for consumer_name, provider_name in (
        ("application", "service"),
        ("service", "storage"),
    ):
        consumer = nodes[consumer_name]
        provider = nodes[provider_name]
        requirement = requirements[consumer_name][0]
        edge = ExecutableComponentEdge(
            consumer.revision.identity,
            provider.revision.identity,
            provider_name,
            requirement.capability,
            dependency_kind,
            provider.interface_bindings[0].interface_identity
            if dependency_kind is DependencyKind.GENERATION
            else None,
        )
        edges.append(edge)
        selection = consumer.target_flavor_selection
        nodes[consumer_name] = replace(
            consumer,
            requirement_constraint_satisfactions=(
                RequirementConstraintSatisfaction(
                    consumer.revision.identity,
                    provider.revision.identity,
                    provider_name,
                    requirement.constraints,
                    selection.target_name,
                    selection.target_profile_identity,
                    selection.selection_policy_identity,
                    selection.identity,
                    identity(f"{consumer_name}-{provider_name}-satisfaction"),
                ),
            ),
        )
    lock = ComponentLock(
        "host",
        identity("invoice-cli-target"),
        identity("selection-policy"),
        identity("resolver"),
        nodes["application"].revision.identity,
        tuple(sorted(nodes.values(), key=lambda item: item.revision.identity.uri)),
        tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.consumer_revision.uri,
                    item.provider_revision.uri,
                    item.requirement_id,
                    item.kind.value,
                ),
            )
        ),
        tuple(sorted(authorings.values(), key=lambda item: item.identity.uri)),
    )
    authority = LockedGenerationAuthority(
        lock,
        authorings["application"],
        lock.authorings,
        (flavor,),
        (),
    )
    snapshot = _Snapshot(
        authority,
        contents,
        {
            flavor_reference.identity.uri: flavor_spec,
            **(
                {
                    flavor_model_reference.identity.uri: flavor_model_content,
                }
                if flavor_model_reference is not None
                and flavor_model_content is not None
                else {}
            ),
        },
    )
    models = {
        item.revision.identity.uri: identity(f"model-{item.revision.coordinate.name}")
        for item in lock.nodes
    }
    return snapshot, plan_component_execution(lock, model_identities=models)


class ComponentNodeGenerationPreparationTests(unittest.TestCase):
    def test_library_recipe_has_import_surface_without_product_entrypoint(self):
        snapshot, execution = _fixture(library_names=frozenset({"service"}))
        service = next(
            item
            for item in execution.generation_plans
            if next(
                node
                for node in snapshot.authority.lock.nodes
                if node.revision.identity == item.component_revision
            ).revision.coordinate.name
            == "service"
        )

        projection = LockedComponentNodePreparationAdapter().project(snapshot, service)

        self.assertIsNotNone(projection.recipe.library_import_surface)
        self.assertEqual(projection.recipe.all_required_entrypoints, ())
        self.assertIn("This Component is a library", projection.recipe.prompt())
        self.assertNotIn("required generated entrypoint", projection.recipe.prompt())

    def test_consumer_recipe_receives_exact_direct_library_import_surface(self):
        snapshot, execution = _fixture(library_names=frozenset({"service"}))
        application = next(
            item
            for item in execution.generation_plans
            if next(
                node
                for node in snapshot.authority.lock.nodes
                if node.revision.identity == item.component_revision
            ).revision.coordinate.name
            == "application"
        )
        projection = LockedComponentNodePreparationAdapter().project(
            snapshot, application
        )

        self.assertEqual(len(projection.recipe.library_dependencies), 1)
        dependency = projection.recipe.library_dependencies[0]
        self.assertEqual(dependency.capability, "service-api")
        self.assertEqual(dependency.import_surface.language, "python")
        self.assertEqual(
            dependency.import_surface.capability("service-api").interface_identity,
            dependency.interface_identity,
        )
        self.assertIn("Required direct library imports", projection.recipe.prompt())

    def test_execution_documents_are_explicit_and_identity_bound(self):
        snapshot, execution = _fixture()
        document = RecipeDocument.create(
            "execution/worker-stack.json",
            '{"schema":"example/worker-stack@1"}',
        )
        adapter = LockedComponentNodePreparationAdapter(execution_documents=(document,))
        plan = execution.generation_plans[0]
        projected = adapter.project(snapshot, plan)

        self.assertIn(document, projected.recipe.documents)
        self.assertIn(document.path, projected.recipe.non_acceptance_document_paths)
        self.assertIn(document.content, projected.recipe.prompt())

    def test_execution_document_paths_must_be_unique_and_sorted(self):
        first = RecipeDocument.create("execution/z.json", "{}")
        second = RecipeDocument.create("execution/a.json", "{}")
        with self.assertRaises(ValueError):
            LockedComponentNodePreparationAdapter(execution_documents=(first, second))

    def test_locked_model_scope_precedence_reaches_selected_skill(self):
        snapshot, _execution = _fixture(
            component_models={"application": "component-model"},
            flavor_model="flavor-model",
            skill_model="skill-model",
        )

        bindings = LockedComponentModelSelectionAdapter(
            pipeline_model="pipeline-model"
        ).bindings(snapshot, coding_cli="codex")
        application = next(
            item
            for item in snapshot.authority.lock.nodes
            if item.revision.coordinate.name == "application"
        )
        binding = bindings[application.revision.identity.uri]

        self.assertEqual(binding.explicit_model, "skill-model")
        self.assertEqual(
            tuple(step.decision for step in binding.resolution_trace),
            (
                ModelScopeDecision.OVERRIDE,
                ModelScopeDecision.OVERRIDE,
                ModelScopeDecision.OVERRIDE,
                ModelScopeDecision.OVERRIDE,
            ),
        )
        self.assertEqual(
            tuple(step.selected_selector for step in binding.resolution_trace),
            (
                "pipeline-model",
                "component-model",
                "flavor-model",
                "skill-model",
            ),
        )

    def test_locked_sibling_component_restores_pipeline_model(self):
        snapshot, _execution = _fixture(
            component_models={"application": "component-model"}
        )

        bindings = LockedComponentModelSelectionAdapter(
            pipeline_model="pipeline-model"
        ).bindings(snapshot, coding_cli="codex")
        by_name = {
            item.revision.coordinate.name: bindings[item.revision.identity.uri]
            for item in snapshot.authority.lock.nodes
        }

        self.assertEqual(by_name["application"].explicit_model, "component-model")
        self.assertEqual(by_name["service"].explicit_model, "pipeline-model")
        self.assertEqual(by_name["storage"].explicit_model, "pipeline-model")
        self.assertEqual(
            by_name["service"].resolution_trace[1].decision,
            ModelScopeDecision.INHERIT,
        )

    def test_component_model_change_invalidates_only_that_node_derivation(self):
        first_snapshot, _first_execution = _fixture(
            component_models={"application": "component-model-a"}
        )
        second_snapshot, _second_execution = _fixture(
            component_models={"application": "component-model-b"}
        )
        selector = LockedComponentModelSelectionAdapter(pipeline_model="pipeline-model")

        def by_name(snapshot):
            bindings = selector.bindings(snapshot, coding_cli="codex")
            execution = plan_component_execution(
                snapshot.authority.lock,
                model_identities={
                    revision: binding.identity for revision, binding in bindings.items()
                },
            )
            plans = {
                item.component_revision.uri: item for item in execution.generation_plans
            }
            return {
                item.revision.coordinate.name: (
                    bindings[item.revision.identity.uri],
                    plans[item.revision.identity.uri].generation_key.identity,
                )
                for item in snapshot.authority.lock.nodes
            }

        first = by_name(first_snapshot)
        second = by_name(second_snapshot)
        self.assertNotEqual(
            first["application"][0].identity,
            second["application"][0].identity,
        )
        self.assertNotEqual(first["application"][1], second["application"][1])
        self.assertEqual(first["service"], second["service"])
        self.assertEqual(first["storage"], second["storage"])

    def test_three_locked_nodes_are_bounded_and_independently_generatable(self):
        snapshot, execution = _fixture()
        adapter = LockedComponentNodePreparationAdapter()
        with tempfile.TemporaryDirectory() as directory:
            allocator = FilesystemComponentWorkspaceAllocator(Path(directory))
            prepared = prepare_component_generation_nodes(
                execution,
                authority=snapshot,
                authority_lock_identity=adapter.authority_lock_identity,
                authority_guard=adapter.guard,
                node_projector=adapter.project,
                workspace_allocator=allocator.allocate,
                framework_envelope=lambda projection: projection.recipe.prompt(
                    include_locked_authority_documents=False
                ).encode("utf-8"),
                budget=_budget(),
            )
            self.assertEqual(len(prepared), 3)
            self.assertEqual(snapshot.guard_count, 5)
            self.assertEqual(
                {
                    dict(item.recipe.resolved_inputs)["component_revision"]
                    for item in prepared
                },
                {item.plan.component_revision.uri for item in prepared},
            )
            self.assertEqual(len({item.workspace.locator for item in prepared}), 3)
            by_name = {item.definition.coordinate.name: item for item in prepared}
            implementation_skill = (
                ROOT
                / "skills/specification-to-source"
                / "portable-application-implementation/SKILL.md"
            ).read_bytes()
            for item in prepared:
                node = next(
                    candidate
                    for candidate in snapshot.authority.lock.nodes
                    if candidate.revision.identity == item.plan.component_revision
                )
                for reference in node.revision.specifications:
                    specification = snapshot.component_content(
                        node.revision.authoring_identity,
                        reference,
                    )
                    self.assertEqual(item.request.prompt.count(specification), 1)
                self.assertIn(
                    b"Generate a portable Python command-line application.\n",
                    item.request.prompt,
                )
                self.assertIn(implementation_skill, item.request.prompt)
            service_interface = b'{"interface":"service-public"}'
            storage_interface = b'{"interface":"storage-public"}'
            self.assertIn(service_interface, by_name["application"].request.prompt)
            self.assertNotIn(storage_interface, by_name["application"].request.prompt)
            self.assertNotIn(
                b"Only implement the service Component.",
                by_name["application"].request.prompt,
            )
            self.assertNotIn(
                b"Only implement the storage Component.",
                by_name["application"].request.prompt,
            )
            self.assertIn(storage_interface, by_name["service"].request.prompt)
            self.assertEqual(
                by_name[
                    "storage"
                ].plan.generation_key.direct_public_interface_identities,
                (),
            )
            for item in prepared:
                self.assertIsNotNone(item.recipe.managed_sbom_graph)
                assert item.recipe.managed_sbom_graph is not None
                expected_managed_names = {
                    "application": {
                        "component://samples/application",
                        "component://samples/service",
                        "component://samples/storage",
                    },
                    "service": {
                        "component://samples/service",
                        "component://samples/storage",
                    },
                    "storage": {"component://samples/storage"},
                }
                self.assertEqual(
                    {
                        component.name
                        for component in item.recipe.managed_sbom_graph.components
                    },
                    expected_managed_names[item.definition.coordinate.name],
                )
                self.assertEqual(
                    item.recipe.managed_sbom_graph.resolved_graph_identity,
                    snapshot.authority.lock.identity,
                )
                self.assertIn(
                    "acceptance/execution.json",
                    {document.path for document in item.recipe.documents},
                )
                self.assertNotIn(
                    "acceptance/execution.json",
                    {document.path for document in item.recipe.model_documents},
                )
                self.assertIn(b'"invocation_signatures"', item.request.prompt)
                workspace = Path(item.workspace.locator)
                self.assertEqual(tuple(workspace.iterdir()), ())
                entrypoint = workspace / item.recipe.all_required_entrypoints[0]
                entrypoint.parent.mkdir(parents=True)
                entrypoint.write_text(
                    f"print('{item.definition.coordinate.name}')\n",
                    encoding="utf-8",
                )
                self.assertTrue(entrypoint.is_file())
                self.assertEqual(
                    item.request.request.component_generation_plan_identity,
                    item.plan.identity,
                )
                self.assertEqual(
                    item.request.request.generation_key_identity,
                    item.plan.generation_key.identity,
                )
                resolved = dict(item.recipe.resolved_inputs)
                self.assertEqual(
                    resolved["model_selection"],
                    item.plan.generation_key.model_identity.uri,
                )
                self.assertEqual(
                    tuple(
                        sorted(skill.identity for skill in item.recipe.resolved_skills)
                    ),
                    tuple(
                        sorted(
                            identity.uri
                            for identity in item.plan.generation_key.skill_identities
                        )
                    ),
                )
                self.assertEqual(
                    tuple(
                        sorted(
                            flavor.revision_identity for flavor in item.recipe.flavors
                        )
                    ),
                    tuple(
                        identity.uri
                        for identity in item.plan.generation_key.flavor_identities
                    ),
                )


if __name__ == "__main__":
    unittest.main()
