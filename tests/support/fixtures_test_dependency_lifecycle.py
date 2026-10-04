from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_dependency_lifecycle``."""


from literate_ai.adapters.dependencies import (
    DependencyObservationError,
    HostDependencyObservation,
    MacOsMachODependencyObserver,
)
from literate_ai.contracts import (
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    canonical_identity,
    component_bom_ref,
)


def _managed_graph() -> CycloneDxManagedGraph:
    identity = canonical_identity({"fixture": "dependency-lifecycle"})
    root_ref = component_bom_ref(identity)
    return CycloneDxManagedGraph(
        root_ref,
        (
            CycloneDxManagedComponent(
                root_ref,
                ManagedComponentKind.ROOT,
                identity,
                "urn:literate-ai:component:fixture/application",
                "1.0.0",
                (),
            ),
        ),
        (),
        canonical_identity({"fixture": "dependency-lifecycle-composition"}),
    )


class _EmptyObserver:
    def observe(self, build, *, root_ref: str) -> HostDependencyObservation:
        del build, root_ref
        return HostDependencyObservation((), ())


class _FixtureMacObserver(MacOsMachODependencyObserver):
    def __init__(self, accepted: set[str]) -> None:
        super().__init__(lifecycle_commands=())
        self.accepted = accepted

    def _run_dyld(self, arguments) -> str:
        if (
            len(arguments) == 2
            and arguments[0] == "-validate_only"
            and arguments[-1] in self.accepted
        ):
            return "valid"
        raise DependencyObservationError("fixture.rejected", "image is absent")
