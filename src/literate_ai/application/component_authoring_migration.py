"""Project legacy welded Component definitions into human-owned authoring intent."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from literate_ai.contracts.capabilities import Capability
from literate_ai.contracts.component_locking.authoring import (
    AuthoredProvidedCapability,
    AuthoredRepositorySourceDependency,
    ComponentAuthoring,
    ComponentContentSelector,
)
from literate_ai.contracts.components import ComponentDefinition
from literate_ai.contracts.identity import ContentIdentity, ContentReference
from literate_ai.contracts.repositories import RepositorySourceDependency


@dataclass(frozen=True, slots=True)
class LoadedRepositoryDependency:
    """A parsed dependency paired with the identity of its observed raw bytes."""

    dependency: RepositorySourceDependency
    observed_content_identity: ContentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.dependency, RepositorySourceDependency):
            raise TypeError("loaded repository dependency must be typed")
        if not isinstance(self.observed_content_identity, ContentIdentity):
            raise TypeError("loaded repository raw content identity must be typed")


RepositoryDependencyLoader = Callable[[ContentReference], LoadedRepositoryDependency]


class ComponentAuthoringMigrationError(ValueError):
    """A welded legacy value cannot be projected without guessing author intent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def project_component_authoring(
    definition: ComponentDefinition,
    *,
    repository_dependencies: Iterable[RepositorySourceDependency] = (),
    capability_interfaces: Mapping[str, ComponentContentSelector] | None = None,
) -> ComponentAuthoring:
    """Purely remove resolver identities while preserving expressible semantics."""

    if not isinstance(definition, ComponentDefinition):
        raise TypeError("Component authoring migration requires ComponentDefinition")
    dependencies = tuple(repository_dependencies)
    if any(not isinstance(item, RepositorySourceDependency) for item in dependencies):
        raise TypeError("repository dependencies must be typed resolved definitions")
    if len(dependencies) != len(definition.source_dependencies):
        raise ComponentAuthoringMigrationError(
            "repository-dependency-count-mismatch",
            "every and only welded repository reference must be loaded",
        )

    interfaces = {} if capability_interfaces is None else dict(capability_interfaces)
    unknown_interfaces = set(interfaces) - {item.name for item in definition.provides}
    if unknown_interfaces:
        raise ComponentAuthoringMigrationError(
            "capability-interface-unknown",
            "authored interfaces name capabilities absent from the legacy definition",
        )
    provided = tuple(
        sorted(
            (
                _provided_capability(item, interfaces.get(item.name))
                for item in definition.provides
            ),
            key=lambda item: item.name,
        )
    )
    authored_dependencies = tuple(
        sorted(
            (_repository_dependency(item) for item in dependencies),
            key=lambda item: item.dependency_id,
        )
    )
    return ComponentAuthoring(
        coordinate=definition.coordinate,
        version=definition.version,
        display_name=definition.display_name,
        description=definition.description,
        profiles=tuple(sorted(definition.profiles)),
        sample=definition.sample,
        provides=provided,
        requires=tuple(
            sorted(definition.requires, key=lambda item: item.requirement_id)
        ),
        specification_provider=definition.specification_provider,
        specification_roots=definition.specification_roots,
        authoring_inputs=tuple(
            sorted(
                (_selector(item) for item in definition.authoring_inputs),
                key=lambda item: (item.kind, item.uri),
            )
        ),
        workflow_definition=_selector(definition.workflow_definition),
        routing_policy=_selector(definition.routing_policy),
        flavor_slots=tuple(
            sorted(definition.flavor_slots, key=lambda item: item.slot_id)
        ),
        entrypoints=tuple(sorted(definition.entrypoints, key=lambda item: item.name)),
        acceptance_contracts=tuple(
            sorted(
                (_selector(item) for item in definition.acceptance_contracts),
                key=lambda item: (item.kind, item.uri),
            )
        ),
        source_dependencies=authored_dependencies,
    )


def migrate_component_authoring(
    definition: ComponentDefinition,
    repository_dependency_loader: RepositoryDependencyLoader,
    *,
    capability_interfaces: Mapping[str, ComponentContentSelector] | None = None,
) -> ComponentAuthoring:
    """Load exact welded repository definitions, then invoke the pure projection."""

    if not isinstance(definition, ComponentDefinition):
        raise TypeError("Component authoring migration requires ComponentDefinition")
    if not callable(repository_dependency_loader):
        raise TypeError("repository dependency loader must be callable")
    # Reject the one genuinely lossy legacy shape before invoking any adapter I/O.
    interfaces = {} if capability_interfaces is None else capability_interfaces
    for capability in definition.provides:
        _provided_capability(capability, interfaces.get(capability.name))
    loaded: list[RepositorySourceDependency] = []
    for reference in definition.source_dependencies:
        try:
            observed = repository_dependency_loader(reference)
        except Exception as exc:
            raise ComponentAuthoringMigrationError(
                "repository-dependency-load-failed",
                f"could not load welded repository dependency {reference.uri!r}",
            ) from exc
        if not isinstance(observed, LoadedRepositoryDependency):
            raise ComponentAuthoringMigrationError(
                "repository-dependency-invalid",
                "repository dependency loader returned an untyped value",
            )
        if observed.observed_content_identity != reference.identity:
            raise ComponentAuthoringMigrationError(
                "repository-dependency-content-identity-mismatch",
                f"loaded repository dependency {reference.uri!r} changed raw bytes",
            )
        loaded.append(observed.dependency)
    return project_component_authoring(
        definition,
        repository_dependencies=tuple(loaded),
        capability_interfaces=interfaces,
    )


def _selector(reference: ContentReference) -> ComponentContentSelector:
    return ComponentContentSelector(reference.kind, reference.uri, None)


def _provided_capability(
    capability: Capability,
    interface: ComponentContentSelector | None = None,
) -> AuthoredProvidedCapability:
    if capability.contract is not None and interface is None:
        raise ComponentAuthoringMigrationError(
            "ambiguous-capability-contract",
            f"capability {capability.name!r} has an identity but no authored "
            "interface URI",
        )
    if capability.contract is None and interface is not None:
        raise ComponentAuthoringMigrationError(
            "capability-interface-ambiguous",
            f"capability {capability.name!r} has an authored interface but no "
            "legacy contract identity",
        )
    return AuthoredProvidedCapability(capability.name, capability.version, interface)


def _repository_dependency(
    dependency: RepositorySourceDependency,
) -> AuthoredRepositorySourceDependency:
    return AuthoredRepositorySourceDependency(
        dependency_id=dependency.dependency_id,
        repository_url=dependency.repository_url,
        revision_selector=dependency.revision_selector,
        dependency_kind=dependency.dependency_kind,
        optional=dependency.optional,
        integration_contract=(
            None
            if dependency.integration_contract is None
            else _selector(dependency.integration_contract)
        ),
    )


__all__ = [
    "ComponentAuthoringMigrationError",
    "LoadedRepositoryDependency",
    "RepositoryDependencyLoader",
    "migrate_component_authoring",
    "project_component_authoring",
]
