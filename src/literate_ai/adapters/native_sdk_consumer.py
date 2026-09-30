"""Bind completed SDK source builds to exact consumer inputs and fresh directories."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.adapters.native_sdk_custody import (
    capture_native_sdk,
    materialize_native_sdk,
)
from literate_ai.adapters.native_sdk_dependencies import (
    NativeSdkDependencyEvidence,
    project_native_sdk_dependencies,
)
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.native_sdk_runtime import observe_native_sdk_runtime
from literate_ai.adapters.native_sdk_source_build import (
    NativeSdkSourceBuild,
    NativeSdkSourceBuildService,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.sbom import (
    ManagedComponentKind,
    project_component_lock_managed_graph,
)
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildAuthorization,
    BuildRequest,
)
from literate_ai.storage import BlobRef


@dataclass(frozen=True, slots=True)
class NativeSdkConsumerInput:
    """Portable input evidence, without execution or transported admission authority."""

    build: NativeSdkSourceBuild

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        built = self.build
        return {
            "schema": "literate-ai/native-sdk-consumer-input@1",
            "consumer_revision": built.selection.component_revision.to_dict(),
            "target_identity": built.selection.target_identity.to_dict(),
            "dependency_id": built.selection.recipe.dependency_id,
            "selection": built.selection.identity.to_dict(),
            "source_admission": built.resolution.admission.to_dict(),
            "sdk_snapshot": built.product.snapshot_blob.to_dict(),
            "runtime_observation": built.product.runtime_observation.to_dict(),
            "build_result": built.product.result_blob.to_dict(),
            "import_surface": built.product.snapshot.import_surface.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MaterializedNativeSdkInput:
    binding: NativeSdkConsumerInput
    root: Path
    runtime_observation: BlobRef
    dependencies: NativeSdkDependencyEvidence

    @property
    def import_root(self) -> Path:
        return self.root / self.binding.build.product.snapshot.import_root


class NativeSdkConsumerInputs:
    """Live producer-backed inputs for every SDK dependency in one locked project.

    Construction never builds. Source-build services must already have completed
    under their own policy. Materialization observes bytes without loading native
    code; Standard consumer authorization and acceptance remain separate boundaries.
    """

    def __init__(
        self,
        *,
        snapshot: LockedGenerationAuthoritySnapshot,
        services: tuple[NativeSdkSourceBuildService, ...],
    ) -> None:
        self.snapshot = snapshot
        self._services = tuple(services)
        expected = {
            selection.identity: selection
            for node in snapshot.authority.lock.nodes
            if node.revision.repository_sources
            for selection in select_native_sdk_recipes(snapshot, node.revision.identity)
        }
        received = {}
        for service in self._services:
            if not isinstance(service, NativeSdkSourceBuildService):
                raise TypeError(
                    "SDK consumer inputs require completed source-build services"
                )
            built = service.completed_result()
            selection = built.selection
            if (
                selection.identity in received
                or expected.get(selection.identity) != selection
                or service.snapshot.authority.lock.identity
                != snapshot.authority.lock.identity
            ):
                raise ValueError(
                    "SDK builds must cover the exact selected consumer dependencies"
                )
            binding = NativeSdkConsumerInput(built)
            service.store.put_manifest(binding.to_dict())
            received[selection.identity] = (service, binding)
        if set(received) != set(expected):
            raise ValueError("SDK builds must cover every selected consumer dependency")
        self._inputs = tuple(
            received[key] for key in sorted(received, key=lambda key: key.uri)
        )
        self._blob_sources = {
            (binding.identity, item.blob): service.store
            for service, binding in self._inputs
            for item in binding.build.product.snapshot.files
        }
        namespaces = set()
        for _service, binding in self._inputs:
            built = binding.build
            surface = built.product.snapshot.import_surface
            key = (
                built.selection.component_revision,
                surface.language,
                surface.package,
            )
            if key in namespaces:
                raise ValueError("SDK consumer import packages conflict")
            namespaces.add(key)
        self.require_unchanged()

    def require_unchanged(self) -> None:
        self.snapshot.require_unchanged()
        for service, binding in self._inputs:
            if service.completed_result() != binding.build:
                raise ValueError("SDK consumer input changed after binding")

    def for_consumer(
        self,
        revision: ContentIdentity,
        *,
        target_identity: ContentIdentity,
    ) -> tuple[NativeSdkConsumerInput, ...]:
        self.require_unchanged()
        if not any(
            node.revision.identity == revision
            for node in self.snapshot.authority.lock.nodes
        ):
            raise ValueError("SDK consumer is absent from the locked project")
        selected = tuple(
            binding
            for _service, binding in self._inputs
            if binding.build.selection.component_revision == revision
        )
        if any(
            binding.build.selection.target_identity != target_identity
            for binding in selected
        ):
            raise ValueError("SDK consumer target differs from its built products")
        return selected

    @contextmanager
    def materialize(
        self,
        revision: ContentIdentity,
        *,
        target_identity: ContentIdentity,
        parent: Path,
    ) -> Iterator[tuple[MaterializedNativeSdkInput, ...]]:
        selected = self.for_consumer(revision, target_identity=target_identity)
        services = {binding.identity: service for service, binding in self._inputs}
        roots = []
        materialized = []
        try:
            for binding in selected:
                service = services[binding.identity]
                sdk = binding.build.product.snapshot
                root = materialize_native_sdk(
                    sdk,
                    expected_identity=sdk.identity,
                    store=service.store,
                    parent=parent,
                )
                roots.append(root)
                layout = binding.build.selection.recipe.layout
                observation = observe_native_sdk_runtime(
                    sdk,
                    expected_identity=sdk.identity,
                    target_identity=target_identity,
                    operating_system=layout.operating_system,
                    architecture=layout.architecture,
                    root=root,
                    store=service.store,
                )
                dependencies = project_native_sdk_dependencies(
                    binding, root=root, runtime=service.store.get_manifest(observation)
                )
                service.store.put_manifest(dependencies.to_dict())
                materialized.append(
                    MaterializedNativeSdkInput(binding, root, observation, dependencies)
                )
            self.require_unchanged()
            yield tuple(materialized)
            self.require_unchanged()
            for value in materialized:
                sdk = value.binding.build.product.snapshot
                service = services[value.binding.identity]
                captured = capture_native_sdk(
                    value.root,
                    store=service.store,
                    source_lock_identity=sdk.source_lock_identity,
                    recipe_identity=sdk.recipe_identity,
                    target_identity=sdk.target_identity,
                    license_identity=sdk.license_identity,
                    import_surface=sdk.import_surface,
                    import_root=sdk.import_root,
                    native_libraries=sdk.native_libraries,
                )
                if captured != sdk:
                    raise ValueError("SDK consumer input changed during use")
        finally:
            for root in reversed(roots):
                shutil.rmtree(root)

    def dependency_evidence(
        self, revision: ContentIdentity, *, parent: Path
    ) -> tuple[NativeSdkDependencyEvidence, ...]:
        """Observe every SDK in this Component's managed dependency closure.

        Transitive SDKs appear in the BOM without exposing their imports to the
        direct consumer. Each temporary materialization is verified and cleaned.
        """
        selected = self.for_scope(revision)
        selections = {
            (
                binding.build.selection.component_revision,
                binding.build.selection.target_identity,
            )
            for binding in selected
        }
        evidence = []
        for consumer, target in sorted(
            selections, key=lambda pair: (pair[0].uri, pair[1].uri)
        ):
            with self.materialize(
                consumer, target_identity=target, parent=parent
            ) as values:
                evidence.extend(value.dependencies for value in values)
        self.require_unchanged()
        return tuple(sorted(evidence, key=lambda value: value.input_identity.uri))

    def for_scope(
        self, revision: ContentIdentity
    ) -> tuple[NativeSdkConsumerInput, ...]:
        """Return SDK bindings for the exact managed Component closure."""
        self.require_unchanged()
        graph = project_component_lock_managed_graph(
            self.snapshot.authority.lock, revision
        )
        revisions = {
            item.identity
            for item in graph.components
            if item.kind in (ManagedComponentKind.ROOT, ManagedComponentKind.COMPONENT)
        }
        return tuple(
            binding
            for _service, binding in self._inputs
            if binding.build.selection.component_revision in revisions
        )

    def read_input_blob(
        self, binding: NativeSdkConsumerInput, reference: BlobRef
    ) -> bytes:
        """Read only declared SDK files.

        Callers bracket a multi-file operation with require_unchanged; each read
        independently verifies its CAS bytes without rescanning the whole SDK.
        """
        store = self._blob_sources.get((binding.identity, reference))
        if store is None:
            raise ValueError("blob is not a declared SDK consumer input")
        return store.get_bytes(reference)

    def reobserve_runtime(self, value: MaterializedNativeSdkInput) -> None:
        """Recheck SDK bytes and the native dependency graph at a launch boundary."""
        self.require_unchanged()
        for service, binding in self._inputs:
            if binding != value.binding:
                continue
            sdk = binding.build.product.snapshot
            layout = binding.build.selection.recipe.layout
            observed = observe_native_sdk_runtime(
                sdk,
                expected_identity=sdk.identity,
                target_identity=sdk.target_identity,
                operating_system=layout.operating_system,
                architecture=layout.architecture,
                root=value.root,
                store=service.store,
            )
            evidence = project_native_sdk_dependencies(
                binding, root=value.root, runtime=service.store.get_manifest(observed)
            )
            if evidence.identity != value.dependencies.identity:
                raise ValueError("SDK runtime dependencies changed before execution")
            return
        raise ValueError("SDK runtime input is not bound to this consumer owner")

    def require_execution_authorized(
        self,
        revision: ContentIdentity,
        request: BuildRequest,
        authorization: BuildAuthorization,
        *,
        now: datetime,
    ) -> tuple[dict[str, object], ...]:
        """Check the command grant against current producer revocations."""
        self.require_unchanged()
        if request.effective_revision_digest != revision.uri:
            raise ValueError("SDK execution request differs from its consumer")
        services = [
            (service, binding)
            for service, binding in self._inputs
            if binding.build.selection.component_revision == revision
        ]
        if not services:
            raise ValueError("SDK execution requires selected consumer inputs")
        evidence = []
        for service, binding in services:
            # Source-build verification also guards temporary captured-source files.
            # Those are gone after completion. Consumer custody was rechecked above;
            # read the same live host revocations without reviving source workspaces.
            current = service.revocations()
            if not isinstance(current, AuthorizationRevocationSet):
                raise AuthorizationError("security.live_revocation_verifier_invalid")
            current.require_build_valid(authorization, request, now=now)
            evidence.append(
                {
                    "input_identity": binding.identity.to_dict(),
                    "state": current.to_dict(),
                }
            )
        return tuple(evidence)

    def execution_revocation_sources(self, revision: ContentIdentity):
        """Capture live host callbacks while exact package input custody is current."""
        selected = self.for_scope(revision)
        return tuple(
            (binding.identity, service.revocations)
            for service, binding in self._inputs
            if binding in selected
        )
