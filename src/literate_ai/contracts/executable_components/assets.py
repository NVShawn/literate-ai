"""Authored asset, candidate replacement, and lifecycle-driver trust contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    int_value,
    string_value,
)
from ..blobs import BlobRef
from ..identity import ContentIdentity, contract_identity
from ..paths import canonical_relative_posix_path
from ._common import identity, optional_identity, portable_name

AUTHORED_BINARY_ASSET_SCHEMA = "urn:literate-ai:schema:v2:authored-binary-asset"
CANDIDATE_REPLACEMENT_POLICY_SCHEMA = (
    "urn:literate-ai:schema:v2:candidate-replacement-policy"
)
LIFECYCLE_DRIVER_TRUST_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v2:lifecycle-driver-trust-binding"
)


class AssetAssemblyMode(StrEnum):
    AUTHORED_IMMUTABLE_OVERLAY = "authored-immutable-overlay"


@dataclass(frozen=True, slots=True)
class AuthoredBinaryAsset:
    """Arbitrary authored bytes assembled outside the model-produced text tree."""

    component_revision: ContentIdentity
    asset_id: str
    path: str
    role: str
    target_identity: ContentIdentity
    blob: BlobRef
    authorization_identity: ContentIdentity
    assembly_mode: AssetAssemblyMode = AssetAssemblyMode.AUTHORED_IMMUTABLE_OVERLAY
    model_writable: bool = False

    SCHEMA: ClassVar[str] = AUTHORED_BINARY_ASSET_SCHEMA

    def __post_init__(self) -> None:
        identity(self.component_revision, "AuthoredBinaryAsset.component_revision")
        portable_name(self.asset_id, "AuthoredBinaryAsset.asset_id")
        try:
            canonical_relative_posix_path(self.path, label="authored binary asset path")
        except (TypeError, ValueError) as exc:
            fail("AuthoredBinaryAsset.path", str(exc))
        portable_name(self.role, "AuthoredBinaryAsset.role")
        identity(self.target_identity, "AuthoredBinaryAsset.target_identity")
        if not isinstance(self.blob, BlobRef):
            fail("AuthoredBinaryAsset.blob", "must be a BlobRef")
        identity(
            self.authorization_identity,
            "AuthoredBinaryAsset.authorization_identity",
        )
        if self.assembly_mode is not AssetAssemblyMode.AUTHORED_IMMUTABLE_OVERLAY:
            fail(
                "AuthoredBinaryAsset.assembly_mode",
                "must keep authored bytes outside the model-writable tree",
            )
        if bool_value(self.model_writable, "AuthoredBinaryAsset.model_writable"):
            fail(
                "AuthoredBinaryAsset.model_writable",
                "authored binary assets cannot be model-writable",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "asset_id": self.asset_id,
            "path": self.path,
            "role": self.role,
            "target_identity": self.target_identity.to_dict(),
            "blob": self.blob.to_dict(),
            "authorization_identity": self.authorization_identity.to_dict(),
            "assembly_mode": self.assembly_mode.value,
            "model_writable": self.model_writable,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "AuthoredBinaryAsset"
    ) -> AuthoredBinaryAsset:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "asset_id",
                    "path",
                    "role",
                    "target_identity",
                    "blob",
                    "authorization_identity",
                    "assembly_mode",
                    "model_writable",
                }
            ),
        )
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            asset_id=portable_name(data["asset_id"], f"{path}.asset_id"),
            path=string_value(data["path"], f"{path}.path"),
            role=portable_name(data["role"], f"{path}.role"),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            blob=BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
            authorization_identity=ContentIdentity.from_dict(
                data["authorization_identity"],
                path=f"{path}.authorization_identity",
            ),
            assembly_mode=enum_value(
                AssetAssemblyMode, data["assembly_mode"], f"{path}.assembly_mode"
            ),
            model_writable=bool_value(data["model_writable"], f"{path}.model_writable"),
        )


class ReplacementScope(StrEnum):
    COMPLETE_TREE = "complete-tree"


class ReplacementWorkspace(StrEnum):
    FRESH_EMPTY = "fresh-empty"


class FailedCandidateDisposition(StrEnum):
    EXTERNAL_EVIDENCE_ONLY = "external-evidence-only"


@dataclass(frozen=True, slots=True)
class CandidateReplacementPolicy:
    """Repair replaces a whole candidate; it never patches accepted source in place."""

    maximum_repairs: int
    replacement_scope: ReplacementScope = ReplacementScope.COMPLETE_TREE
    workspace: ReplacementWorkspace = ReplacementWorkspace.FRESH_EMPTY
    authored_assets: AssetAssemblyMode = AssetAssemblyMode.AUTHORED_IMMUTABLE_OVERLAY
    failed_candidates: FailedCandidateDisposition = (
        FailedCandidateDisposition.EXTERNAL_EVIDENCE_ONLY
    )

    SCHEMA: ClassVar[str] = CANDIDATE_REPLACEMENT_POLICY_SCHEMA

    def __post_init__(self) -> None:
        int_value(
            self.maximum_repairs,
            "CandidateReplacementPolicy.maximum_repairs",
            minimum=0,
            maximum=2,
        )
        if self.replacement_scope is not ReplacementScope.COMPLETE_TREE:
            fail(
                "CandidateReplacementPolicy.replacement_scope",
                "must replace the complete candidate tree",
            )
        if self.workspace is not ReplacementWorkspace.FRESH_EMPTY:
            fail(
                "CandidateReplacementPolicy.workspace",
                "must allocate a fresh empty workspace",
            )
        if self.authored_assets is not AssetAssemblyMode.AUTHORED_IMMUTABLE_OVERLAY:
            fail(
                "CandidateReplacementPolicy.authored_assets",
                "must preserve the immutable authored-asset overlay",
            )
        if (
            self.failed_candidates
            is not FailedCandidateDisposition.EXTERNAL_EVIDENCE_ONLY
        ):
            fail(
                "CandidateReplacementPolicy.failed_candidates",
                "failed candidates may remain only in external runtime evidence",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "maximum_repairs": self.maximum_repairs,
            "replacement_scope": self.replacement_scope.value,
            "workspace": self.workspace.value,
            "authored_assets": self.authored_assets.value,
            "failed_candidates": self.failed_candidates.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CandidateReplacementPolicy"
    ) -> CandidateReplacementPolicy:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "maximum_repairs",
                    "replacement_scope",
                    "workspace",
                    "authored_assets",
                    "failed_candidates",
                }
            ),
        )
        return cls(
            maximum_repairs=int_value(
                data["maximum_repairs"],
                f"{path}.maximum_repairs",
                minimum=0,
                maximum=2,
            ),
            replacement_scope=enum_value(
                ReplacementScope,
                data["replacement_scope"],
                f"{path}.replacement_scope",
            ),
            workspace=enum_value(
                ReplacementWorkspace, data["workspace"], f"{path}.workspace"
            ),
            authored_assets=enum_value(
                AssetAssemblyMode,
                data["authored_assets"],
                f"{path}.authored_assets",
            ),
            failed_candidates=enum_value(
                FailedCandidateDisposition,
                data["failed_candidates"],
                f"{path}.failed_candidates",
            ),
        )


class LifecycleDriverTrust(StrEnum):
    STANDARD = "standard"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class LifecycleDriverTrustBinding:
    """Distinguish framework-distributed and project-authorized lifecycle code."""

    trust: LifecycleDriverTrust
    driver_identity: ContentIdentity
    policy_identity: ContentIdentity
    framework_distribution_identity: ContentIdentity | None
    project_authorization_identity: ContentIdentity | None

    SCHEMA: ClassVar[str] = LIFECYCLE_DRIVER_TRUST_BINDING_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.trust, LifecycleDriverTrust):
            fail(
                "LifecycleDriverTrustBinding.trust",
                "must be a LifecycleDriverTrust",
            )
        identity(self.driver_identity, "LifecycleDriverTrustBinding.driver_identity")
        identity(self.policy_identity, "LifecycleDriverTrustBinding.policy_identity")
        if self.trust is LifecycleDriverTrust.STANDARD:
            identity(
                self.framework_distribution_identity,
                "LifecycleDriverTrustBinding.framework_distribution_identity",
            )
            if self.project_authorization_identity is not None:
                fail(
                    "LifecycleDriverTrustBinding.project_authorization_identity",
                    "must be absent for a standard driver",
                )
        else:
            identity(
                self.project_authorization_identity,
                "LifecycleDriverTrustBinding.project_authorization_identity",
            )
            if self.framework_distribution_identity is not None:
                fail(
                    "LifecycleDriverTrustBinding.framework_distribution_identity",
                    "must be absent for an external driver",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        def optional(value: ContentIdentity | None) -> object:
            return None if value is None else value.to_dict()

        return {
            "schema": self.SCHEMA,
            "trust": self.trust.value,
            "driver_identity": self.driver_identity.to_dict(),
            "policy_identity": self.policy_identity.to_dict(),
            "framework_distribution_identity": optional(
                self.framework_distribution_identity
            ),
            "project_authorization_identity": optional(
                self.project_authorization_identity
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LifecycleDriverTrustBinding"
    ) -> LifecycleDriverTrustBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "trust",
                    "driver_identity",
                    "policy_identity",
                    "framework_distribution_identity",
                    "project_authorization_identity",
                }
            ),
        )
        return cls(
            trust=enum_value(LifecycleDriverTrust, data["trust"], f"{path}.trust"),
            driver_identity=ContentIdentity.from_dict(
                data["driver_identity"], path=f"{path}.driver_identity"
            ),
            policy_identity=ContentIdentity.from_dict(
                data["policy_identity"], path=f"{path}.policy_identity"
            ),
            framework_distribution_identity=optional_identity(
                data["framework_distribution_identity"],
                f"{path}.framework_distribution_identity",
            ),
            project_authorization_identity=optional_identity(
                data["project_authorization_identity"],
                f"{path}.project_authorization_identity",
            ),
        )


__all__ = [
    "AUTHORED_BINARY_ASSET_SCHEMA",
    "CANDIDATE_REPLACEMENT_POLICY_SCHEMA",
    "LIFECYCLE_DRIVER_TRUST_BINDING_SCHEMA",
    "AssetAssemblyMode",
    "AuthoredBinaryAsset",
    "CandidateReplacementPolicy",
    "FailedCandidateDisposition",
    "LifecycleDriverTrust",
    "LifecycleDriverTrustBinding",
    "ReplacementScope",
    "ReplacementWorkspace",
]
