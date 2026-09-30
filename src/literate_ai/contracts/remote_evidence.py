"""Portable custody contracts for evidence produced by a remote lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    fail,
    int_value,
    list_value,
    optional_string,
    string_value,
)
from .identity import ContentIdentity, canonical_identity
from .library_products import LibraryArtifactProduct

REMOTE_EVIDENCE_FILE_SCHEMA = "urn:literate-ai:schema:v1:remote-evidence-file"
REMOTE_FAILURE_DIAGNOSTIC_SCHEMA = "urn:literate-ai:schema:v1:remote-failure-diagnostic"
REMOTE_LIFECYCLE_EVIDENCE_MANIFEST_SCHEMA = (
    "urn:literate-ai:schema:v1:remote-lifecycle-evidence-manifest"
)
REMOTE_EVIDENCE_CUSTODY_RECEIPT_SCHEMA = (
    "urn:literate-ai:schema:v1:remote-evidence-custody-receipt"
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PORTABLE_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_KINDS = frozenset(
    {
        "artifact",
        "binary",
        "diagnostic",
        "log",
        "manifest",
        "object",
        "package",
        "receipt",
        "source",
        "test",
    }
)


def _identity(value: Any, path: str) -> ContentIdentity:
    try:
        return ContentIdentity.from_dict(value, path=path)
    except (TypeError, ValueError) as exc:
        fail(path, str(exc))


@dataclass(frozen=True, slots=True)
class RemoteEvidenceFile:
    path: str
    kind: str
    size: int
    identity: ContentIdentity
    executable: bool = False

    SCHEMA: ClassVar[str] = REMOTE_EVIDENCE_FILE_SCHEMA

    def __post_init__(self) -> None:
        if not _PORTABLE_PATH.fullmatch(self.path) or ".." in self.path.split("/"):
            fail(
                "RemoteEvidenceFile.path", "must be a canonical relative portable path"
            )
        if self.kind not in _KINDS:
            fail("RemoteEvidenceFile.kind", "is not an allowed evidence kind")
        int_value(self.size, "RemoteEvidenceFile.size", maximum=2**63 - 1)
        if not isinstance(self.identity, ContentIdentity):
            fail("RemoteEvidenceFile.identity", "must be a ContentIdentity")
        bool_value(self.executable, "RemoteEvidenceFile.executable")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "identity": self.identity.to_dict(),
            "executable": self.executable,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RemoteEvidenceFile"
    ) -> RemoteEvidenceFile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"path", "kind", "size", "identity", "executable"}),
        )
        return cls(
            string_value(data["path"], f"{path}.path", max_length=4096),
            string_value(data["kind"], f"{path}.kind", max_length=32),
            int_value(data["size"], f"{path}.size", maximum=2**63 - 1),
            _identity(data["identity"], f"{path}.identity"),
            bool_value(data["executable"], f"{path}.executable"),
        )


@dataclass(frozen=True, slots=True)
class RemoteFailureDiagnostic:
    code: str
    message: str
    nested_code: str | None = None
    nested_message: str | None = None
    stdout: str = ""
    stderr: str = ""
    redaction_policy: str = "literate-ai-secret-private-paths-v1"

    SCHEMA: ClassVar[str] = REMOTE_FAILURE_DIAGNOSTIC_SCHEMA

    def __post_init__(self) -> None:
        for value, path, limit in (
            (self.code, "RemoteFailureDiagnostic.code", 256),
            (self.message, "RemoteFailureDiagnostic.message", 8192),
            (self.redaction_policy, "RemoteFailureDiagnostic.redaction_policy", 128),
        ):
            string_value(value, path, max_length=limit)
        if (self.nested_code is None) != (self.nested_message is None):
            fail(
                "RemoteFailureDiagnostic.nested_message",
                "nested code and message must appear together",
            )
        if self.nested_code is not None:
            string_value(
                self.nested_code,
                "RemoteFailureDiagnostic.nested_code",
                max_length=256,
            )
            string_value(
                self.nested_message,
                "RemoteFailureDiagnostic.nested_message",
                max_length=8192,
            )
        string_value(
            self.stdout,
            "RemoteFailureDiagnostic.stdout",
            nonempty=False,
            max_length=65536,
        )
        string_value(
            self.stderr,
            "RemoteFailureDiagnostic.stderr",
            nonempty=False,
            max_length=65536,
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "code": self.code,
            "message": self.message,
            "nested_code": self.nested_code,
            "nested_message": self.nested_message,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "redaction_policy": self.redaction_policy,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RemoteFailureDiagnostic"
    ) -> RemoteFailureDiagnostic:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "code",
                    "message",
                    "nested_code",
                    "nested_message",
                    "stdout",
                    "stderr",
                    "redaction_policy",
                }
            ),
        )
        return cls(
            string_value(data["code"], f"{path}.code", max_length=256),
            string_value(data["message"], f"{path}.message", max_length=8192),
            optional_string(data["nested_code"], f"{path}.nested_code"),
            optional_string(data["nested_message"], f"{path}.nested_message"),
            string_value(
                data["stdout"],
                f"{path}.stdout",
                nonempty=False,
                max_length=65536,
            ),
            string_value(
                data["stderr"],
                f"{path}.stderr",
                nonempty=False,
                max_length=65536,
            ),
            string_value(
                data["redaction_policy"], f"{path}.redaction_policy", max_length=128
            ),
        )


@dataclass(frozen=True, slots=True)
class RemoteLifecycleEvidenceManifest:
    request_identity: ContentIdentity
    authority_identity: ContentIdentity
    worker_identity: ContentIdentity
    materialization_identity: ContentIdentity
    source_identity: ContentIdentity
    specification_identity: ContentIdentity
    flavor_identity: ContentIdentity
    toolchain_identity: ContentIdentity
    source_index_identity: ContentIdentity
    model_scope_identity: ContentIdentity
    action: str
    outcome: str
    result_evidence_identity: ContentIdentity
    observed_environment_identity: ContentIdentity
    artifact_identity: ContentIdentity | None
    artifact_is_directory: bool | None
    stage_identities: tuple[ContentIdentity, ...]
    files: tuple[RemoteEvidenceFile, ...]
    failure: RemoteFailureDiagnostic | None = None
    cleanup_token: str = ""
    library_product: LibraryArtifactProduct | None = None

    SCHEMA: ClassVar[str] = REMOTE_LIFECYCLE_EVIDENCE_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        identities = (
            self.request_identity,
            self.authority_identity,
            self.worker_identity,
            self.materialization_identity,
            self.source_identity,
            self.specification_identity,
            self.flavor_identity,
            self.toolchain_identity,
            self.source_index_identity,
            self.model_scope_identity,
            self.result_evidence_identity,
            self.observed_environment_identity,
            *self.stage_identities,
        )
        if any(not isinstance(item, ContentIdentity) for item in identities):
            fail("RemoteLifecycleEvidenceManifest", "all identities must be typed")
        if self.action not in {"build", "test", "run"}:
            fail(
                "RemoteLifecycleEvidenceManifest.action", "must be build, test, or run"
            )
        if self.outcome not in {"passed", "failed", "cancelled", "timed-out"}:
            fail("RemoteLifecycleEvidenceManifest.outcome", "is invalid")
        if (self.artifact_identity is None) != (self.artifact_is_directory is None):
            fail(
                "RemoteLifecycleEvidenceManifest.artifact_identity",
                "artifact identity and shape must appear together",
            )
        if self.artifact_identity is not None and not isinstance(
            self.artifact_identity, ContentIdentity
        ):
            fail(
                "RemoteLifecycleEvidenceManifest.artifact_identity",
                "must be a ContentIdentity",
            )
        if self.artifact_is_directory is not None:
            bool_value(
                self.artifact_is_directory,
                "RemoteLifecycleEvidenceManifest.artifact_is_directory",
            )
        if (self.outcome == "passed") != (self.failure is None):
            fail(
                "RemoteLifecycleEvidenceManifest.failure",
                "must be absent only for passing evidence",
            )
        if self.failure is not None and not isinstance(
            self.failure, RemoteFailureDiagnostic
        ):
            fail("RemoteLifecycleEvidenceManifest.failure", "must be typed")
        stage_uris = tuple(item.uri for item in self.stage_identities)
        if stage_uris != tuple(sorted(set(stage_uris))):
            fail(
                "RemoteLifecycleEvidenceManifest.stage_identities",
                "must be uniquely sorted",
            )
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(set(paths))):
            fail(
                "RemoteLifecycleEvidenceManifest.files", "must be uniquely path-sorted"
            )
        if any(not isinstance(item, RemoteEvidenceFile) for item in self.files):
            fail("RemoteLifecycleEvidenceManifest.files", "must contain typed files")
        if not re.fullmatch(r"[0-9a-f]{32}", self.cleanup_token):
            fail(
                "RemoteLifecycleEvidenceManifest.cleanup_token",
                "must be a 128-bit lower-case hex token",
            )
        if self.library_product is not None:
            if (
                not isinstance(self.library_product, LibraryArtifactProduct)
                or self.action not in {"build", "test"}
                or self.outcome != "passed"
                or self.artifact_is_directory is not True
            ):
                fail(
                    "RemoteLifecycleEvidenceManifest.library_product",
                    "requires a typed accepted directory library build/test product",
                )
            package = next(
                (item for item in self.files if item.path == "package/library.zip"),
                None,
            )
            blob = self.library_product.artifact_export.blob
            if (
                package is None
                or package.kind != "package"
                or package.executable
                or package.identity.digest != blob.digest
                or package.size != blob.size
            ):
                fail(
                    "RemoteLifecycleEvidenceManifest.library_product",
                    "must bind the exact sealed package evidence",
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity.to_dict(),
            "authority_identity": self.authority_identity.to_dict(),
            "worker_identity": self.worker_identity.to_dict(),
            "materialization_identity": self.materialization_identity.to_dict(),
            "source_identity": self.source_identity.to_dict(),
            "specification_identity": self.specification_identity.to_dict(),
            "flavor_identity": self.flavor_identity.to_dict(),
            "toolchain_identity": self.toolchain_identity.to_dict(),
            "source_index_identity": self.source_index_identity.to_dict(),
            "model_scope_identity": self.model_scope_identity.to_dict(),
            "action": self.action,
            "outcome": self.outcome,
            "result_evidence_identity": self.result_evidence_identity.to_dict(),
            "observed_environment_identity": (
                self.observed_environment_identity.to_dict()
            ),
            "artifact_identity": (
                None
                if self.artifact_identity is None
                else self.artifact_identity.to_dict()
            ),
            "artifact_is_directory": self.artifact_is_directory,
            "stage_identities": [item.to_dict() for item in self.stage_identities],
            "files": [item.to_dict() for item in self.files],
            "failure": None if self.failure is None else self.failure.to_dict(),
            "cleanup_token": self.cleanup_token,
            **(
                {}
                if self.library_product is None
                else {"library_artifact": self.library_product.to_dict()}
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RemoteLifecycleEvidenceManifest"
    ) -> RemoteLifecycleEvidenceManifest:
        names = {
            "request_identity",
            "authority_identity",
            "worker_identity",
            "materialization_identity",
            "source_identity",
            "specification_identity",
            "flavor_identity",
            "toolchain_identity",
            "source_index_identity",
            "model_scope_identity",
            "action",
            "outcome",
            "result_evidence_identity",
            "observed_environment_identity",
            "artifact_identity",
            "artifact_is_directory",
            "stage_identities",
            "files",
            "failure",
            "cleanup_token",
        }
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(names),
            optional=frozenset({"library_artifact"}),
        )
        identity_names = (
            "request_identity",
            "authority_identity",
            "worker_identity",
            "materialization_identity",
            "source_identity",
            "specification_identity",
            "flavor_identity",
            "toolchain_identity",
            "source_index_identity",
            "model_scope_identity",
            "result_evidence_identity",
            "observed_environment_identity",
        )
        identities = {
            name: _identity(data[name], f"{path}.{name}") for name in identity_names
        }
        return cls(
            identities["request_identity"],
            identities["authority_identity"],
            identities["worker_identity"],
            identities["materialization_identity"],
            identities["source_identity"],
            identities["specification_identity"],
            identities["flavor_identity"],
            identities["toolchain_identity"],
            identities["source_index_identity"],
            identities["model_scope_identity"],
            string_value(data["action"], f"{path}.action"),
            string_value(data["outcome"], f"{path}.outcome"),
            identities["result_evidence_identity"],
            identities["observed_environment_identity"],
            (
                None
                if data["artifact_identity"] is None
                else _identity(data["artifact_identity"], f"{path}.artifact_identity")
            ),
            (
                None
                if data["artifact_is_directory"] is None
                else bool_value(
                    data["artifact_is_directory"], f"{path}.artifact_is_directory"
                )
            ),
            tuple(
                _identity(item, f"{path}.stage_identities[{index}]")
                for index, item in enumerate(
                    list_value(data["stage_identities"], f"{path}.stage_identities")
                )
            ),
            tuple(
                RemoteEvidenceFile.from_dict(item, path=f"{path}.files[{index}]")
                for index, item in enumerate(list_value(data["files"], f"{path}.files"))
            ),
            (
                None
                if data["failure"] is None
                else RemoteFailureDiagnostic.from_dict(
                    data["failure"], path=f"{path}.failure"
                )
            ),
            string_value(data["cleanup_token"], f"{path}.cleanup_token"),
            None
            if "library_artifact" not in data
            else LibraryArtifactProduct.from_dict(
                data["library_artifact"], path=f"{path}.library_artifact"
            ),
        )


@dataclass(frozen=True, slots=True)
class RemoteEvidenceCustodyReceipt:
    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    manifest_identity: ContentIdentity
    bundle_identity: ContentIdentity
    coordinator_store_identity: ContentIdentity
    imported_file_identities: tuple[ContentIdentity, ...]
    verified_artifact_identity: ContentIdentity | None
    cleanup_acknowledgement_identity: ContentIdentity

    SCHEMA: ClassVar[str] = REMOTE_EVIDENCE_CUSTODY_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        values = (
            self.request_identity,
            self.worker_identity,
            self.manifest_identity,
            self.bundle_identity,
            self.coordinator_store_identity,
            *self.imported_file_identities,
        )
        if any(not isinstance(item, ContentIdentity) for item in values):
            fail("RemoteEvidenceCustodyReceipt", "all identities must be typed")
        if self.verified_artifact_identity is not None and not isinstance(
            self.verified_artifact_identity, ContentIdentity
        ):
            fail(
                "RemoteEvidenceCustodyReceipt.verified_artifact_identity",
                "must be a ContentIdentity",
            )
        if not isinstance(self.cleanup_acknowledgement_identity, ContentIdentity):
            fail(
                "RemoteEvidenceCustodyReceipt.cleanup_acknowledgement_identity",
                "must be a ContentIdentity",
            )
        uris = tuple(item.uri for item in self.imported_file_identities)
        if uris != tuple(sorted(set(uris))):
            fail(
                "RemoteEvidenceCustodyReceipt.imported_file_identities",
                "must be uniquely sorted",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity.to_dict(),
            "worker_identity": self.worker_identity.to_dict(),
            "manifest_identity": self.manifest_identity.to_dict(),
            "bundle_identity": self.bundle_identity.to_dict(),
            "coordinator_store_identity": self.coordinator_store_identity.to_dict(),
            "imported_file_identities": [
                item.to_dict() for item in self.imported_file_identities
            ],
            "verified_artifact_identity": (
                None
                if self.verified_artifact_identity is None
                else self.verified_artifact_identity.to_dict()
            ),
            "cleanup_acknowledgement_identity": (
                self.cleanup_acknowledgement_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RemoteEvidenceCustodyReceipt"
    ) -> RemoteEvidenceCustodyReceipt:
        names = {
            "request_identity",
            "worker_identity",
            "manifest_identity",
            "bundle_identity",
            "coordinator_store_identity",
            "imported_file_identities",
            "verified_artifact_identity",
            "cleanup_acknowledgement_identity",
        }
        data = contract_fields(
            value, path=path, schema_uri=cls.SCHEMA, required=frozenset(names)
        )
        return cls(
            _identity(data["request_identity"], f"{path}.request_identity"),
            _identity(data["worker_identity"], f"{path}.worker_identity"),
            _identity(data["manifest_identity"], f"{path}.manifest_identity"),
            _identity(data["bundle_identity"], f"{path}.bundle_identity"),
            _identity(
                data["coordinator_store_identity"],
                f"{path}.coordinator_store_identity",
            ),
            tuple(
                _identity(item, f"{path}.imported_file_identities[{index}]")
                for index, item in enumerate(
                    list_value(
                        data["imported_file_identities"],
                        f"{path}.imported_file_identities",
                    )
                )
            ),
            (
                None
                if data["verified_artifact_identity"] is None
                else _identity(
                    data["verified_artifact_identity"],
                    f"{path}.verified_artifact_identity",
                )
            ),
            _identity(
                data["cleanup_acknowledgement_identity"],
                f"{path}.cleanup_acknowledgement_identity",
            ),
        )


__all__ = [
    "REMOTE_EVIDENCE_CUSTODY_RECEIPT_SCHEMA",
    "REMOTE_EVIDENCE_FILE_SCHEMA",
    "REMOTE_FAILURE_DIAGNOSTIC_SCHEMA",
    "REMOTE_LIFECYCLE_EVIDENCE_MANIFEST_SCHEMA",
    "RemoteEvidenceCustodyReceipt",
    "RemoteEvidenceFile",
    "RemoteFailureDiagnostic",
    "RemoteLifecycleEvidenceManifest",
]
