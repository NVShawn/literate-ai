"""Current canonical Component references expressed in admitted-source coordinates.

This operation-local context grants no execution or package admission. Standalone
root-relative planning remains a separate explicit lower-level contract.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from literate_ai.adapters.component_lock_application import (
    current_project_component_lock_bindings,
)
from literate_ai.adapters.component_lock_planning import _component_reference_path
from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.contracts.identity import canonical_json_bytes

UNIT_REQUEST_SUFFIX = ".package-unit.json"
_BOUND_REFERENCES = object()


@dataclass(frozen=True, init=False, eq=False)
class RetainedUnitSpecificationContext:
    """Qualified references from current selected catalog owners; no decoder."""

    source: Path
    manifest_identity: object
    bindings: tuple
    references: Mapping
    memory_bytes: int

    def __init__(
        self,
        source,
        manifest_identity,
        bindings,
        references,
        memory_bytes,
        *,
        _seal=None,
    ):
        if _seal is not _BOUND_REFERENCES:
            raise TypeError("retained.unit.current-reference-context-required")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "manifest_identity", manifest_identity)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "references", MappingProxyType(dict(references)))
        object.__setattr__(self, "memory_bytes", memory_bytes)

    def require_current(self, source, manifest, locks):
        if (
            Path(source).resolve(strict=True) != self.source
            or manifest.identity != self.manifest_identity
            or tuple(lock for lock, _catalog in self.bindings) != locks
        ):
            raise ValueError("retained.unit.reference-context-changed")
        checked = set()
        for lock, catalog in self.bindings:
            key = (id(catalog._authorings), id(catalog._flavors))
            if key not in checked:
                catalog.require_unchanged()
                checked.add(key)
            ComponentLockStore(catalog.root).require_current(lock)

    def path_for(self, lock, reference):
        key = (lock.identity, reference.uri, reference.identity)
        try:
            return self.references[key]
        except KeyError:
            raise ValueError("retained.unit.unbound-locked-specification") from None


def bind_retained_unit_specifications(
    project,
    source,
    manifest,
    locks,
    *,
    maximum_units,
    maximum_memory_bytes,
):
    """Qualify exact locked declarations through their real selected Components.

    References are resolved once with the canonical kind-aware boundary/link
    checks. Neither basename nor content equality can select another source path.
    """
    if (
        type(maximum_units) is not int
        or not 1 <= maximum_units <= 1_000_000
        or type(maximum_memory_bytes) is not int
        or not 1 <= maximum_memory_bytes <= 16 * 1024**3
    ):
        raise ValueError("retained.unit.reference-policy-invalid")
    memory = 128 * (len(manifest.members) + len(locks))
    if memory > maximum_memory_bytes:
        raise ValueError("retained.unit.reference-memory-policy-exceeded")
    source = Path(source).resolve(strict=True)
    bindings = current_project_component_lock_bindings(project)
    if tuple(lock for lock, _catalog in bindings) != locks:
        raise ValueError("retained.unit.current-reference-lock-set-changed")
    reference_memory = 128 * len(bindings)
    charged = set()
    for lock, catalog in bindings:
        key = (id(catalog._authorings), id(catalog._flavors))
        if key not in charged:
            reference_memory += catalog.retained_memory_estimate()
            charged.add(key)
        reference_memory += 8192 + 16 * len(canonical_json_bytes(lock.to_dict()))
        if 128 * len(manifest.members) + reference_memory > maximum_memory_bytes:
            raise ValueError("retained.unit.reference-memory-policy-exceeded")
    memory = 128 * len(manifest.members) + reference_memory
    members = {member.path: member for member in manifest.members}
    references = {}
    for lock, catalog in bindings:
        revision = next(
            node.revision
            for node in lock.nodes
            if node.revision.identity == lock.root_revision
        )
        if (
            catalog.project is None
            or catalog.project.root != project.root
            or catalog.root_authoring != revision.definition
        ):
            raise ValueError("retained.unit.registered-reference-owner-required")
        for reference in revision.specifications:
            if not reference.uri.endswith(UNIT_REQUEST_SUFFIX):
                continue
            path = _component_reference_path(
                catalog.root, reference, boundary=catalog.boundary
            )
            try:
                qualified = path.relative_to(source).as_posix()
            except ValueError:
                raise ValueError(
                    "retained.unit.reference-outside-admitted-source"
                ) from None
            charge = 4096 + 4 * len(qualified.encode("utf-8"))
            memory += charge
            reference_memory += charge
            if len(references) >= maximum_units or memory > maximum_memory_bytes:
                raise ValueError("retained.unit.reference-memory-policy-exceeded")
            member = members.get(qualified)
            if (
                member is None
                or member.symlink_target is not None
                or member.mode != 0o644
                or member.size > 8 * 1024**2
                or member.sha256 != reference.identity.digest
            ):
                raise ValueError("retained.unit.locked-specification-source-mismatch")
            key = (lock.identity, reference.uri, reference.identity)
            if key in references:
                raise ValueError("retained.unit.ambiguous-locked-specification")
            references[key] = qualified
    if not references:
        raise ValueError("retained.unit.locked-package-unit-specifications-required")
    result = RetainedUnitSpecificationContext(
        source,
        manifest.identity,
        bindings,
        references,
        reference_memory,
        _seal=_BOUND_REFERENCES,
    )
    result.require_current(source, manifest, locks)
    return result
