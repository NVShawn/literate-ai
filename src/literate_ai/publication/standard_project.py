"""Top-level package and publication orchestration for one accepted Standard project."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from literate_ai.application.packaging import PackageAdapter, PackagingError
from literate_ai.application.release_artifacts import (
    StandardReleaseDeclaration,
    create_standard_artifact_build_graph,
    create_standard_package_plan,
    create_standard_release_artifact_set,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleResult,
)
from literate_ai.contracts import (
    BlobRef,
    ComponentLock,
    ContentIdentity,
    canonical_identity,
)
from literate_ai.contracts._validation import contract_fields, parse_tuple
from literate_ai.contracts.executable_components import (
    ComponentExecutionPlan,
    PackageFileKind,
    PackageKind,
    PackagePlan,
    PackageResult,
    ReleaseArtifactSet,
    SourceBundleClosure,
)
from literate_ai.security import SecurityProfile

from .filesystem import (
    FilesystemPublicationTarget,
    PublicationManifest,
    PublicationPolicy,
    PublicationRequest,
    PublicationService,
    TransferReceipt,
)
from .release import (
    create_publication_request_from_release,
    validate_transfer_receipt_for_release,
)

ReleaseBlobReader = Callable[[BlobRef], bytes]


class StandardProjectReleaseError(RuntimeError):
    """Packaging, custody ingestion, authorization, or publication failed closed."""


@dataclass(frozen=True, slots=True)
class StandardProjectReleaseReceipt:
    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:standard-project-release-receipt"

    component_lock_identity: ContentIdentity
    execution_plan_identity: ContentIdentity
    lifecycle_receipt_identity: ContentIdentity
    artifact_graph_identity: ContentIdentity
    release_declaration_identities: tuple[ContentIdentity, ...]
    release_artifact_set_identity: ContentIdentity
    publication_request_identity: ContentIdentity
    transfer_receipt_identity: ContentIdentity
    root_component_revision: ContentIdentity
    target_identity: ContentIdentity

    def __post_init__(self) -> None:
        identities = (
            self.component_lock_identity,
            self.execution_plan_identity,
            self.lifecycle_receipt_identity,
            self.artifact_graph_identity,
            self.release_artifact_set_identity,
            self.publication_request_identity,
            self.transfer_receipt_identity,
            self.root_component_revision,
            self.target_identity,
        )
        if any(not isinstance(item, ContentIdentity) for item in identities):
            raise TypeError("release receipt identities must be typed")
        if (
            not self.release_declaration_identities
            or any(
                not isinstance(item, ContentIdentity)
                for item in self.release_declaration_identities
            )
            or self.release_declaration_identities
            != tuple(
                sorted(
                    set(self.release_declaration_identities), key=lambda item: item.uri
                )
            )
        ):
            raise ValueError(
                "release declaration identities must be non-empty, unique, and sorted"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "lifecycle_receipt_identity": self.lifecycle_receipt_identity.to_dict(),
            "artifact_graph_identity": self.artifact_graph_identity.to_dict(),
            "release_declaration_identities": [
                item.to_dict() for item in self.release_declaration_identities
            ],
            "release_artifact_set_identity": (
                self.release_artifact_set_identity.to_dict()
            ),
            "publication_request_identity": self.publication_request_identity.to_dict(),
            "transfer_receipt_identity": self.transfer_receipt_identity.to_dict(),
            "root_component_revision": self.root_component_revision.to_dict(),
            "target_identity": self.target_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardProjectReleaseReceipt"
    ) -> StandardProjectReleaseReceipt:
        names = frozenset(
            {
                "component_lock_identity",
                "execution_plan_identity",
                "lifecycle_receipt_identity",
                "artifact_graph_identity",
                "release_declaration_identities",
                "release_artifact_set_identity",
                "publication_request_identity",
                "transfer_receipt_identity",
                "root_component_revision",
                "target_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)

        def parsed(name: str) -> ContentIdentity:
            return ContentIdentity.from_dict(data[name], path=f"{path}.{name}")

        return cls(
            component_lock_identity=parsed("component_lock_identity"),
            execution_plan_identity=parsed("execution_plan_identity"),
            lifecycle_receipt_identity=parsed("lifecycle_receipt_identity"),
            artifact_graph_identity=parsed("artifact_graph_identity"),
            release_declaration_identities=parse_tuple(
                data["release_declaration_identities"],
                f"{path}.release_declaration_identities",
                ContentIdentity.from_dict,
            ),
            release_artifact_set_identity=parsed("release_artifact_set_identity"),
            publication_request_identity=parsed("publication_request_identity"),
            transfer_receipt_identity=parsed("transfer_receipt_identity"),
            root_component_revision=parsed("root_component_revision"),
            target_identity=parsed("target_identity"),
        )


@dataclass(frozen=True, slots=True)
class StandardProjectReleaseResult:
    lifecycle: StandardProjectLifecycleResult
    package_executions: tuple[tuple[PackagePlan, PackageResult], ...]
    release: ReleaseArtifactSet
    request: PublicationRequest
    transfer_receipt: TransferReceipt
    receipt: StandardProjectReleaseReceipt


@dataclass(slots=True)
class StandardProjectReleaseService:
    publication: PublicationService
    policy: PublicationPolicy
    target: FilesystemPublicationTarget
    packagers: Mapping[PackageKind, PackageAdapter]

    def publish_accepted(
        self,
        execution_plan: ComponentExecutionPlan,
        lifecycle: StandardProjectLifecycleResult,
        component_lock: ComponentLock,
        declarations: tuple[StandardReleaseDeclaration, ...],
        *,
        root_source_bundle: SourceBundleClosure,
        evidence_manifest: BlobRef,
        read_blob: ReleaseBlobReader,
        security_classification_identity: ContentIdentity,
        security_profile: SecurityProfile,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> StandardProjectReleaseResult:
        if not lifecycle.successful or lifecycle.receipt_identity is None:
            raise StandardProjectReleaseError(
                "publication requires a successful accepted Standard lifecycle"
            )
        if not declarations:
            raise StandardProjectReleaseError(
                "publication requires a release declaration"
            )
        graph = create_standard_artifact_build_graph(execution_plan, lifecycle)
        results: list[tuple[StandardReleaseDeclaration, PackageResult]] = []
        executions: list[tuple[PackagePlan, PackageResult]] = []
        created: dict[str, bytes] = {}
        for declaration in declarations:
            plan = create_standard_package_plan(execution_plan, graph, declaration)
            try:
                adapter = self.packagers[declaration.package_kind]
                result = adapter.package(plan, read_blob=read_blob)
            except (KeyError, PackagingError) as exc:
                raise StandardProjectReleaseError(
                    "declared package could not be produced"
                ) from exc
            for artifact in result.artifacts:
                if artifact.kind is not PackageFileKind.PACKAGE_OUTPUT:
                    continue
                reader = getattr(adapter, "read_created_blob", None)
                if not callable(reader):
                    raise StandardProjectReleaseError(
                        "package output has no immutable byte custody"
                    )
                created[artifact.blob.identity] = reader(artifact.blob)
            results.append((declaration, result))
            executions.append((plan, result))

        def release_reader(reference: BlobRef) -> bytes:
            content = created.get(reference.identity)
            return read_blob(reference) if content is None else content

        release = create_standard_release_artifact_set(
            execution_plan,
            lifecycle,
            component_lock,
            graph,
            results,
            root_source_bundle=root_source_bundle,
            evidence_manifest=evidence_manifest,
            read_blob=release_reader,
        )
        request = create_publication_request_from_release(
            release,
            component_lock=component_lock,
            security_classification_identity=security_classification_identity,
            security_profile=security_profile,
            target_id=self.target.target_id,
            target_identity=ContentIdentity.parse_uri(self.target.identity),
            policy_identity=ContentIdentity.parse_uri(self.policy.policy_digest),
            actor=actor,
        )
        issued = (now or datetime.now(UTC)).astimezone(UTC)
        authorization = self.policy.authorize(
            request,
            reason=reason,
            issued_at=issued,
            expires_at=issued
            + min(self.policy.maximum_authorization_lifetime, timedelta(minutes=5)),
        )
        for reference in request.blobs:
            stored = self.publication.cas.put_bytes(
                release_reader(reference), media_type=reference.media_type
            )
            if stored != reference:
                raise StandardProjectReleaseError(
                    "publication CAS ingestion changed a release BlobRef"
                )
        manifest = PublicationManifest.create(
            request=request, authorization=authorization, created_at=issued
        )
        transfer = validate_transfer_receipt_for_release(
            request, self.publication.publish(manifest, self.target)
        )
        receipt = StandardProjectReleaseReceipt(
            component_lock.identity,
            execution_plan.identity,
            lifecycle.receipt_identity,
            graph.identity,
            release.release_declaration_identities,
            release.identity,
            canonical_identity(request.to_dict()),
            canonical_identity(transfer.to_dict()),
            execution_plan.root_revision,
            release.target_identity,
        )
        return StandardProjectReleaseResult(
            lifecycle, tuple(executions), release, request, transfer, receipt
        )


__all__ = [
    "StandardProjectReleaseError",
    "StandardProjectReleaseReceipt",
    "StandardProjectReleaseResult",
    "StandardProjectReleaseService",
]
