"""Project an exact release closure into the immutable publication protocol."""

from __future__ import annotations

from literate_ai.contracts import (
    ComponentLock,
    ComponentRevisionRef,
    ContentIdentity,
    ReleaseArtifactSet,
)
from literate_ai.security import SecurityProfile

from .filesystem import PublicationRequest, TransferReceipt


class ReleasePublicationError(ValueError):
    """A publication input does not belong to the selected release closure."""


def validate_transfer_receipt_for_release(
    request: PublicationRequest, receipt: TransferReceipt
) -> TransferReceipt:
    """Reject a transfer receipt that does not exactly close its release request."""

    if not isinstance(request, PublicationRequest):
        raise TypeError("request must be a PublicationRequest")
    if not isinstance(receipt, TransferReceipt):
        raise TypeError("receipt must be a TransferReceipt")
    expected = (
        receipt.direction == "publish"
        and receipt.component_ref == request.component_ref
        and receipt.component_lock_identity == request.component_lock_identity
        and receipt.effective_revision_digest == request.effective_revision_digest
        and receipt.source_bundle == request.source_bundle
        and receipt.provenance == request.provenance
        and receipt.target_id == request.target_id
        and receipt.target_identity_digest == request.target_identity_digest
        and receipt.security_classification_digest
        == request.security_classification_digest
        and receipt.security_profile == request.security_profile
        and receipt.publication_request_digest == request.digest
        and receipt.publication_policy_digest == request.policy_digest
    )
    if not expected:
        raise ReleasePublicationError(
            "publication transfer receipt differs from the exact release request"
        )
    return receipt


def create_publication_request_from_release(
    release: ReleaseArtifactSet,
    *,
    component_lock: ComponentLock,
    security_classification_identity: ContentIdentity,
    security_profile: SecurityProfile,
    target_id: str,
    target_identity: ContentIdentity,
    policy_identity: ContentIdentity,
    actor: str,
    publisher_id: str = "publisher:filesystem@1",
) -> PublicationRequest:
    """Derive publication roots, lock, revision, target, and evidence from a release."""

    if not isinstance(release, ReleaseArtifactSet):
        raise TypeError("release must be a ReleaseArtifactSet")
    if not isinstance(component_lock, ComponentLock):
        raise TypeError("component_lock must be a ComponentLock")
    if component_lock.identity != release.component_lock_identity:
        raise ReleasePublicationError(
            "publication Component lock differs from the release"
        )
    root_node = next(
        (
            item
            for item in component_lock.nodes
            if item.revision.identity == component_lock.root_revision
        ),
        None,
    )
    if root_node is None:
        raise ReleasePublicationError("publication Component lock has no root node")
    expected_ref = ComponentRevisionRef(
        root_node.revision.coordinate,
        root_node.revision.version,
        root_node.revision.identity,
    )
    if release.root_component_ref != expected_ref:
        raise ReleasePublicationError(
            "publication Component ref differs from the exact locked root"
        )
    if not isinstance(security_classification_identity, ContentIdentity):
        raise TypeError("security_classification_identity must be a ContentIdentity")
    if not isinstance(policy_identity, ContentIdentity):
        raise TypeError("policy_identity must be a ContentIdentity")
    if not isinstance(target_identity, ContentIdentity):
        raise TypeError("target_identity must be a ContentIdentity")

    roots = {"source": release.root_source_bundle.root}
    package_blobs = [item.blob for item in release.root_source_bundle.files]
    for package_index, package in enumerate(release.packages):
        for artifact_index, artifact in enumerate(package.artifacts):
            roots[f"package-{package_index:03d}-{artifact_index:03d}"] = artifact.blob
        package_blobs.extend(item.blob for item in package.files)
        package_blobs.extend(item.blob for item in package.artifacts)

    return PublicationRequest.create(
        component_ref=release.root_component_ref,
        component_lock_identity=release.component_lock_identity,
        effective_revision_digest=release.root_component_revision.uri,
        source_bundle=release.root_source_bundle.root,
        roots=roots,
        blobs=(*package_blobs, release.evidence_manifest),
        provenance=(release.evidence_manifest,),
        security_classification_digest=security_classification_identity.uri,
        security_profile=security_profile,
        target_id=target_id,
        target_identity_digest=target_identity.uri,
        policy_digest=policy_identity.uri,
        actor=actor,
        publisher_id=publisher_id,
    )


__all__ = [
    "ReleasePublicationError",
    "create_publication_request_from_release",
    "validate_transfer_receipt_for_release",
]
