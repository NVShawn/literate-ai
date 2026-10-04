"""Shared test fixtures extracted from test_component_node_generation_preparation."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,  # noqa: F401
    LockedComponentNodePreparationAdapter,  # noqa: F401
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
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
