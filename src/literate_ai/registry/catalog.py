"""In-memory reference registry over descriptors, never source checkouts."""

from __future__ import annotations

from collections.abc import Iterable

from literate_ai.contracts import ComponentCoordinate, ComponentRevisionRef

from .models import ComponentDescriptor, DescriptorVocabulary, FlavorDescriptor


class RegistryConflictError(ValueError):
    pass


class RegistryAmbiguityError(LookupError):
    """A legacy logical/version lookup matched multiple immutable revisions."""

    pass


class DescriptorRegistry:
    def __init__(
        self,
        components: Iterable[ComponentDescriptor] = (),
        flavors: Iterable[FlavorDescriptor] = (),
    ) -> None:
        self._components: dict[str, ComponentDescriptor] = {}
        self._flavors: dict[str, FlavorDescriptor] = {}
        self._component_coordinates: dict[str, list[str]] = {}
        for descriptor in components:
            self.register_component(descriptor)
        for descriptor in flavors:
            self.register_flavor(descriptor)

    def register_component(self, descriptor: ComponentDescriptor) -> None:
        key = descriptor.revision_identity.uri
        existing = self._components.get(key)
        if existing is not None and existing != descriptor:
            raise RegistryConflictError(
                f"component revision {key} has conflicting metadata"
            )
        self._components[key] = descriptor
        revisions = self._component_coordinates.setdefault(descriptor.coordinate, [])
        if key not in revisions:
            revisions.append(key)

    def register_flavor(self, descriptor: FlavorDescriptor) -> None:
        key = descriptor.revision_identity.uri
        existing = self._flavors.get(key)
        if existing is not None and existing != descriptor:
            raise RegistryConflictError(
                f"Flavor revision {key} has conflicting metadata"
            )
        self._flavors[key] = descriptor

    def vocabulary(self) -> DescriptorVocabulary:
        # Registration order is explicit catalog order. No filesystem or source
        # access occurs.
        return DescriptorVocabulary(
            tuple(self._components.values()), tuple(self._flavors.values())
        )

    def component(self, revision_uri: str) -> ComponentDescriptor:
        try:
            return self._components[revision_uri]
        except KeyError as error:
            raise KeyError(f"unknown component revision {revision_uri}") from error

    def component_exact(self, reference: ComponentRevisionRef) -> ComponentDescriptor:
        """Resolve an exact ref and reject forged coordinate/version bindings."""

        descriptor = self.component(reference.revision_identity.uri)
        if descriptor.ref != reference:
            raise RegistryConflictError(
                f"component ref {reference.uri} does not match registered metadata"
            )
        return descriptor

    def component_revisions(
        self, coordinate: ComponentCoordinate | str
    ) -> tuple[ComponentDescriptor, ...]:
        coordinate_uri = coordinate
        if isinstance(coordinate, ComponentCoordinate):
            coordinate_uri = coordinate.uri
        return tuple(
            self._components[revision]
            for revision in self._component_coordinates.get(coordinate_uri, ())
        )

    def resolve_component(
        self,
        coordinate: ComponentCoordinate | str,
        *,
        version: str | None = None,
    ) -> ComponentDescriptor:
        """Compatibility lookup that never treats registration order as latest."""

        candidates = self.component_revisions(coordinate)
        if version is not None:
            candidates = tuple(
                item for item in candidates if item.definition.version == version
            )
        if not candidates:
            raise KeyError(f"unknown component {coordinate!s}")
        if len(candidates) != 1:
            raise RegistryAmbiguityError(
                f"component {coordinate!s} requires an exact revision reference"
            )
        return candidates[0]

    def component_providers(self, capability: str) -> tuple[ComponentDescriptor, ...]:
        return tuple(
            descriptor
            for descriptor in self._components.values()
            if any(item.name == capability for item in descriptor.definition.provides)
        )

    def flavors(self) -> tuple[FlavorDescriptor, ...]:
        return tuple(self._flavors.values())


__all__ = [
    "DescriptorRegistry",
    "RegistryAmbiguityError",
    "RegistryConflictError",
]
