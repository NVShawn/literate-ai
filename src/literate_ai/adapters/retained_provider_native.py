"""Resolve current native commands and independent oracle before archive admission."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from literate_ai.adapters.component_acceptance import LibraryAcceptance
from literate_ai.adapters.qualification import _qualification_acceptance_oracle
from literate_ai.adapters.retained_provider_authority import RetainedProviderAuthority
from literate_ai.adapters.standard_project import (
    ProjectedStandardToolchainClosure,
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import ComponentCommandContract, ContentIdentity
from literate_ai.projects import PinnedInputClosure


@dataclass(frozen=True, slots=True)
class RetainedProviderNative:
    provider: RetainedProviderAuthority
    toolchain: ProjectedStandardToolchainClosure
    oracle: LibraryAcceptance
    _oracle_identity: ContentIdentity = field(repr=False)
    _inputs: PinnedInputClosure = field(repr=False, compare=False)

    @property
    def commands(self) -> Mapping[ContentIdentity, ComponentCommandContract]:
        return MappingProxyType(
            {item.component_revision: item for item in self.toolchain.contracts}
        )

    def require_unchanged(self) -> None:
        self.provider.require_unchanged()
        self._inputs.require_unchanged()
        self.toolchain.require_unchanged()
        if self.oracle.identity != self._oracle_identity:
            raise ValueError("retained.provider.oracle-changed")
        self.provider.require_unchanged()
        self._inputs.require_unchanged()
        self.toolchain.require_unchanged()


def read_retained_provider_native(
    provider: RetainedProviderAuthority,
    *,
    environment: Mapping[str, str] | None = None,
) -> RetainedProviderNative:
    """Measure native tools and bind the current verifier-owned library oracle.

    Tool discovery may invoke installed host tools. This never invokes a coding
    CLI, allocates a rebuild runtime, executes generated code or admits an archive.
    Importer review, store selection and retained consumer gates remain separate.
    """
    if not isinstance(provider, RetainedProviderAuthority):
        raise TypeError("retained native inputs require current provider authority")
    provider.require_unchanged()
    generation = provider.generation
    snapshot = generation.prepared.locked_authority_snapshot
    if snapshot.authority.root_authoring.resolved_kind != "library":
        raise ValueError("retained.provider.library-required")
    toolchain = project_locked_standard_toolchain_closure(
        snapshot, generation.execution, environment=environment
    )
    inputs = PinnedInputClosure(
        maximum_files=2,
        maximum_file_bytes=1024 * 1024,
        maximum_total_bytes=2 * 1024 * 1024,
    )
    root = provider.project.root

    def read_bytes(path: Path) -> bytes:
        return inputs.pin(path, boundary=root, label=path.relative_to(root).as_posix())

    oracle = _qualification_acceptance_oracle(
        provider.project,
        generation.prepared,
        provider.qualification.profile,
        read_bytes=read_bytes,
    )
    if not isinstance(oracle, LibraryAcceptance):
        raise TypeError("retained provider requires a library acceptance oracle")
    lock = snapshot.authority.lock
    node = next(n for n in lock.nodes if n.revision.identity == lock.root_revision)
    contract = next(
        item
        for item in toolchain.contracts
        if item.component_revision == lock.root_revision
    )
    surface = contract.library_import_surface
    if (
        surface is None
        or oracle.component != node.revision.coordinate.name
        or oracle.specification_set_identity != node.revision.specification_set_identity
        or oracle.public_interface_identities
        != tuple(
            sorted(
                (i.identity for i in node.revision.public_interfaces),
                key=lambda i: i.uri,
            )
        )
        or oracle.import_surface_identity != surface.identity
        or oracle.language != surface.language
        or any(
            case.capability not in {c.capability for c in surface.capabilities}
            for case in oracle.cases
        )
    ):
        raise ValueError("retained.provider.oracle-authority-mismatch")
    current = RetainedProviderNative(
        provider, toolchain, oracle, oracle.identity, inputs
    )
    current.require_unchanged()
    return current
