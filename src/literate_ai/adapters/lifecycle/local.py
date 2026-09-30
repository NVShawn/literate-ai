"""Standard-library local adapters that make the generation lifecycle executable."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.builders import (
    CppToolchain,
    GuardedCppBuilder,
    GuardedJavaScriptBuilder,
    GuardedPythonBuilder,
    GuardedRustBuilder,
    GuardedRustJavaScriptBuilder,
    GuardedSwiftBuilder,
    NodeToolchain,
    PythonToolchain,
    RustJavaScriptToolchain,
    RustToolchain,
    discover_node_toolchain,
    discover_python_toolchain,
    discover_rust_javascript_toolchain,
    discover_rust_toolchain,
)
from literate_ai.adapters.intelligence import (
    SourceIntelligenceArtifact,
    SourceIntelligenceProviderSelection,
)
from literate_ai.contracts import (
    CycloneDxBomBinding,
    SourceIntelligenceMode,
    canonical_identity,
)
from literate_ai.ports import BuildInputConsumption
from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
    FindingSeverity,
    OriginAttestation,
    RuleBasedSourceScanner,
    SecurityClassification,
    SecurityFinding,
    SecurityPolicy,
    SourceModule,
)
from literate_ai.storage import AppendOnlyEventStore
from literate_ai.validation import ValidationPipeline
from literate_ai.workspace import PreparedTree, WorkspaceTreeStore

from ._paths import (
    canonical_relative_posix_path,
    canonical_relative_posix_paths,
    contained_materialization_target,
    require_materialized_file_contained,
)


def _files(artifact: Mapping[str, object]) -> dict[str, bytes]:
    value = artifact.get("files")
    if not isinstance(value, Mapping) or not value:
        raise ValueError("lifecycle artifact requires a non-empty files object")
    for path, content in value.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise TypeError("lifecycle file paths and contents must be strings")
    paths = canonical_relative_posix_paths(value.keys(), label="lifecycle file path")
    return {
        path.as_posix(): content.encode("utf-8")
        for path, content in zip(paths, value.values(), strict=True)
    }


def _build_request(value: Mapping[str, object]) -> BuildRequest:
    fields = {
        key: value[key]
        for key in (
            "effective_revision_digest",
            "source_bundle_digest",
            "builder_id",
            "toolchain_digest",
            "sandbox_profile",
            "requested_privileges",
            "allowed_outputs",
        )
    }
    if "schema" in value:
        fields["schema"] = value["schema"]
    return BuildRequest.from_dict(fields)


def _lifecycle_consumed_files(artifact: Mapping[str, object]) -> tuple[str, ...]:
    """Return exact generated files admitted by another lifecycle stage."""

    paths: list[str] = []
    for field_name, label in (
        ("generated_test_suite_path", "generated test-suite lifecycle input"),
        ("source_sbom_path", "source SBOM lifecycle input"),
    ):
        path = artifact.get(field_name)
        if path is None:
            continue
        if not isinstance(path, str):
            raise ValueError(f"lifecycle artifact has an invalid {label} path")
        paths.append(canonical_relative_posix_path(path, label=label).as_posix())
    if len(paths) != len(set(paths)):
        raise ValueError("lifecycle artifact input paths must be unique")
    return tuple(paths)


def _classification(value: Mapping[str, object]) -> SecurityClassification:
    return SecurityClassification.from_dict(
        {
            key: value[key]
            for key in (
                "effective_revision_digest",
                "source_digests",
                "dependency_classification_digests",
                "origin_attestation_digests",
                "finding_ids",
                "policy_digest",
                "profile",
                "maximum_severity",
                "permitted_privileges",
                "decision_reason",
            )
        }
    )


def _authorization(value: Mapping[str, object]) -> BuildAuthorization:
    return BuildAuthorization.from_dict(
        {
            key: value[key]
            for key in (
                "authorization_id",
                "classification_digest",
                "request_digest",
                "effective_revision_digest",
                "actor",
                "reason",
                "profile",
                "privileges",
                "issued_at",
                "expires_at",
                "warning",
                "revoked",
            )
        }
    )


@dataclass(slots=True)
class PipelineValidatorAdapter:
    """Expose a typed validation pipeline through the application JSON port."""

    pipeline: ValidationPipeline
    validator_id: str = "validator:pipeline@1"

    def validate(self, artifact: Mapping[str, object]) -> Mapping[str, object]:
        report = self.pipeline.validate(_files(artifact))
        return {
            "validator_ids": list(report.validator_ids),
            "categories": list(report.categories),
            "findings": [
                {
                    **asdict(finding),
                    "severity": finding.severity.value,
                    "evidence_ids": list(finding.evidence_ids),
                }
                for finding in report.findings
            ],
            "passed": report.passed,
        }


@dataclass(slots=True)
class GeneratedTreeSecurityClassifier:
    """Scan generated files and classify their exact, provenance-backed tree."""

    policy: SecurityPolicy
    scanner: RuleBasedSourceScanner
    generator_identity: str
    trust_root: str
    classifier_id: str = "classifier:generated-tree@1"

    def classify(
        self,
        source: Mapping[str, object],
        findings: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]:
        files = _files(source)
        modules = tuple(
            SourceModule.create(path, content) for path, content in files.items()
        )
        report = self.scanner.scan(modules)
        source_digest = str(source["source_bundle_digest"])
        attestation = OriginAttestation(
            source_digest=source_digest,
            signer=self.generator_identity,
            trust_root=self.trust_root,
            signature_identity=canonical_identity(
                {
                    "generator": self.generator_identity,
                    "tree": source_digest,
                    "effective_revision": source["effective_revision_digest"],
                }
            ).uri,
            verified=True,
        )
        validation_findings = tuple(
            SecurityFinding(
                finding_id=f"validation:{index}",
                source_digest=source_digest,
                category=str(item.get("category", "validation")),
                severity=(
                    FindingSeverity.HIGH
                    if item.get("severity") == "error"
                    else FindingSeverity.LOW
                ),
                scanner_identity="validator:application-pipeline",
                message=str(
                    item.get("message", item.get("code", "validation finding"))
                ),
            )
            for index, item in enumerate(findings)
        )
        classification = self.policy.classify(
            effective_revision_digest=str(source["effective_revision_digest"]),
            attestations=(attestation,),
            findings=(*report.findings, *validation_findings),
        )
        result = classification.to_dict()
        result["classification_digest"] = classification.digest
        result["scan_report_digest"] = report.digest
        return result


@dataclass(slots=True)
class PolicyBuildAuthorizer:
    """Issue short-lived identity-scoped build grants from one security policy."""

    policy: SecurityPolicy
    actor: str
    reason: str
    authorization_lifetime: timedelta = timedelta(minutes=10)
    yolo_acknowledged: bool = False
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def authorize(
        self,
        classification: Mapping[str, object],
        build_request: Mapping[str, object],
    ) -> Mapping[str, object]:
        issued_at = self.clock()
        authorization = self.policy.authorize_build(
            _classification(classification),
            _build_request(build_request),
            actor=self.actor,
            reason=self.reason,
            issued_at=issued_at,
            expires_at=issued_at + self.authorization_lifetime,
            yolo_acknowledged=self.yolo_acknowledged,
        )
        return authorization.to_dict()


@dataclass(slots=True)
class PythonBuildAdapter:
    """Materialize proposed source only inside a temporary guarded build input."""

    artifact_store: Path
    toolchain: PythonToolchain = field(default_factory=discover_python_toolchain)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    builder_id: str = GuardedPythonBuilder.builder_id
    authorization_verifier: BuildAuthorizationVerifier = (
        FailClosedBuildAuthorizationVerifier()
    )

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        artifact = request.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("concrete builder requires the generated artifact")
        files = _files(artifact)
        self.artifact_store.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="literate-source-") as directory:
            root = Path(directory)
            _materialize_files(root, files)
            built = GuardedPythonBuilder(
                self.authorization_verifier, toolchain=self.toolchain
            ).build(
                _build_request(request),
                _authorization(authorization),
                source_root=root,
                artifact_store=self.artifact_store,
                now=self.clock(),
            )
        return {
            "artifact_digest": built.artifact_digest,
            "artifact_path": str(built.artifact_path),
            "source_bundle_digest": built.source_bundle_digest,
            "authorization_id": built.authorization_id,
            "toolchain_identity": built.toolchain_identity,
            "compiled_files": list(built.compiled_files),
        }


@dataclass(slots=True)
class CppBuildAdapter:
    """Materialize generated C++ only inside an authorized temporary build input."""

    artifact_store: Path
    toolchain: CppToolchain
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    builder_id: str = GuardedCppBuilder.builder_id
    authorization_verifier: BuildAuthorizationVerifier = (
        FailClosedBuildAuthorizationVerifier()
    )

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        artifact = request.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("concrete builder requires the generated artifact")
        files = _files(artifact)
        self.artifact_store.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="literate-cpp-source-") as directory:
            root = Path(directory)
            _materialize_files(root, files)
            built = GuardedCppBuilder(
                self.toolchain, self.authorization_verifier
            ).build(
                _build_request(request),
                _authorization(authorization),
                source_root=root,
                artifact_store=self.artifact_store,
                now=self.clock(),
            )
        return {
            "artifact_digest": built.artifact_digest,
            "artifact_path": str(built.artifact_path),
            "source_bundle_digest": built.source_bundle_digest,
            "authorization_id": built.authorization_id,
            "executable_file": built.executable_file,
            "compiler_identity": built.compiler_identity,
            "compiled_files": [built.executable_file],
        }


@dataclass(slots=True)
class RustBuildAdapter:
    """Materialize and compile a whole generated Rust application tree."""

    artifact_store: Path
    toolchain: RustToolchain = field(default_factory=discover_rust_toolchain)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    builder_id: str = GuardedRustBuilder.builder_id
    authorization_verifier: BuildAuthorizationVerifier = (
        FailClosedBuildAuthorizationVerifier()
    )

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._build(request, authorization, build_input_consumption=None)

    def build_with_input_consumption(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
        consumption: BuildInputConsumption,
    ) -> Mapping[str, object]:
        if not isinstance(consumption, BuildInputConsumption):
            raise TypeError("Rust builder requires typed build input consumption")
        return self._build(request, authorization, build_input_consumption=consumption)

    def _build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
        *,
        build_input_consumption: BuildInputConsumption | None,
    ) -> Mapping[str, object]:
        artifact = request.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("concrete builder requires the generated artifact")
        files = _files(artifact)
        self.artifact_store.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="literate-rust-source-") as directory:
            root = Path(directory)
            _materialize_files(root, files)
            built = GuardedRustBuilder(
                self.toolchain, self.authorization_verifier
            ).build(
                _build_request(request),
                _authorization(authorization),
                source_root=root,
                artifact_store=self.artifact_store,
                now=self.clock(),
                lifecycle_consumed_files=_lifecycle_consumed_files(artifact),
                build_input_consumption=build_input_consumption,
            )
        return {
            "artifact_digest": built.artifact_digest,
            "artifact_path": str(built.artifact_path),
            "source_bundle_digest": built.source_bundle_digest,
            "authorization_id": built.authorization_id,
            "executable_file": built.executable_file,
            "compiler_identity": built.compiler_identity,
            "consumed_source_files": list(built.consumed_source_files),
            "lifecycle_consumed_files": list(built.lifecycle_consumed_files),
            "build_system_consumed_files": list(built.build_system_consumed_files),
            "build_input_consumption_identity": (
                built.build_input_consumption_identity
            ),
            "compiled_files": [built.executable_file],
        }


@dataclass(slots=True)
class SwiftBuildAdapter:
    """Materialize and compile generated Swift through the guarded host adapter."""

    artifact_store: Path
    toolchain: CppToolchain
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    builder_id: str = GuardedSwiftBuilder.builder_id
    authorization_verifier: BuildAuthorizationVerifier = (
        FailClosedBuildAuthorizationVerifier()
    )

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        artifact = request.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("concrete builder requires the generated artifact")
        files = _files(artifact)
        self.artifact_store.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="literate-swift-source-") as directory:
            root = Path(directory)
            _materialize_files(root, files)
            built = GuardedSwiftBuilder(
                self.toolchain, self.authorization_verifier
            ).build(
                _build_request(request),
                _authorization(authorization),
                source_root=root,
                artifact_store=self.artifact_store,
                now=self.clock(),
            )
        return {
            "artifact_digest": built.artifact_digest,
            "artifact_path": str(built.artifact_path),
            "source_bundle_digest": built.source_bundle_digest,
            "authorization_id": built.authorization_id,
            "executable_file": built.executable_file,
            "compiler_identity": built.compiler_identity,
            "compiled_files": [built.executable_file],
        }


@dataclass(slots=True)
class JavaScriptBuildAdapter:
    """Materialize and syntax-check a whole generated JavaScript application tree."""

    artifact_store: Path
    toolchain: NodeToolchain = field(default_factory=discover_node_toolchain)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    builder_id: str = GuardedJavaScriptBuilder.builder_id
    authorization_verifier: BuildAuthorizationVerifier = (
        FailClosedBuildAuthorizationVerifier()
    )

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        artifact = request.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("concrete builder requires the generated artifact")
        files = _files(artifact)
        self.artifact_store.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="literate-javascript-source-"
        ) as directory:
            root = Path(directory)
            _materialize_files(root, files)
            built = GuardedJavaScriptBuilder(
                self.toolchain, self.authorization_verifier
            ).build(
                _build_request(request),
                _authorization(authorization),
                source_root=root,
                artifact_store=self.artifact_store,
                now=self.clock(),
            )
        return {
            "artifact_digest": built.artifact_digest,
            "artifact_path": str(built.artifact_path),
            "source_bundle_digest": built.source_bundle_digest,
            "authorization_id": built.authorization_id,
            "entrypoint_file": built.entrypoint_file,
            "checked_files": list(built.checked_files),
            "runtime_command": list(built.runtime_command),
            "toolchain_identity": built.toolchain_identity,
            "compiled_files": list(built.checked_files),
        }


@dataclass(slots=True)
class RustJavaScriptBuildAdapter:
    """Build both generated application roles through one guarded authority seam."""

    artifact_store: Path
    toolchain: RustJavaScriptToolchain = field(
        default_factory=discover_rust_javascript_toolchain
    )
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    builder_id: str = GuardedRustJavaScriptBuilder.builder_id
    authorization_verifier: BuildAuthorizationVerifier = (
        FailClosedBuildAuthorizationVerifier()
    )

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._build(request, authorization, build_input_consumption=None)

    def build_with_input_consumption(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
        consumption: BuildInputConsumption,
    ) -> Mapping[str, object]:
        if not isinstance(consumption, BuildInputConsumption):
            raise TypeError("full-stack builder requires typed build input consumption")
        return self._build(request, authorization, build_input_consumption=consumption)

    def _build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
        *,
        build_input_consumption: BuildInputConsumption | None,
    ) -> Mapping[str, object]:
        artifact = request.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValueError("concrete builder requires the generated artifact")
        files = _files(artifact)
        self.artifact_store.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="literate-rust-javascript-source-"
        ) as directory:
            root = Path(directory)
            _materialize_files(root, files)
            built = GuardedRustJavaScriptBuilder(
                self.toolchain, self.authorization_verifier
            ).build(
                _build_request(request),
                _authorization(authorization),
                source_root=root,
                artifact_store=self.artifact_store,
                now=self.clock(),
                lifecycle_consumed_files=_lifecycle_consumed_files(artifact),
                build_input_consumption=build_input_consumption,
            )
        return {
            "artifact_digest": built.artifact_digest,
            "artifact_path": str(built.artifact_path),
            "source_bundle_digest": built.source_bundle_digest,
            "authorization_id": built.authorization_id,
            "backend_executable_file": built.backend_executable_file,
            "frontend_entrypoint_file": built.frontend_entrypoint_file,
            "compiler_identity": built.compiler_identity,
            "runtime_identity": built.runtime_identity,
            "toolchain_identity": built.toolchain_identity,
            "runtime_command": list(built.runtime_command),
            "compiled_files": list(built.compiled_files),
            "consumed_backend_files": list(built.consumed_backend_files),
            "checked_frontend_files": list(built.checked_frontend_files),
            "lifecycle_consumed_files": list(built.lifecycle_consumed_files),
            "build_system_consumed_files": list(built.build_system_consumed_files),
            "build_input_consumption_identity": (
                built.build_input_consumption_identity
            ),
        }


def _materialize_files(root: Path, files: Mapping[str, bytes]) -> None:
    for name, content in files.items():
        relative = canonical_relative_posix_path(name, label="lifecycle file path")
        path = contained_materialization_target(root, relative, label="lifecycle file")
        path.write_bytes(content)
        require_materialized_file_contained(root, path, label="lifecycle file")


class WorkspaceTreeAdapter:
    """Translate immutable workspace trees to the mapping-based application port."""

    def __init__(
        self,
        store: WorkspaceTreeStore,
        *,
        source_intelligence: SourceIntelligenceProviderSelection,
        source_intelligence_custody: object | None = None,
    ) -> None:
        self.store = store
        self.source_intelligence = source_intelligence
        self.source_intelligence_custody = source_intelligence_custody
        self._prepared: dict[str, PreparedTree] = {}

    def prepare(
        self,
        files: Mapping[str, bytes],
        dependency_resolution: Mapping[str, object],
    ) -> Mapping[str, object]:
        prepared = self.store.prepare(files)
        self._prepared[prepared.tree_digest] = prepared
        resolution, source_binding, resolved_binding = _workspace_dependency_identities(
            dependency_resolution
        )
        return {
            "tree_digest": prepared.tree_digest,
            "manifest": [list(item) for item in prepared.manifest],
            "dependency_resolution_identity": resolution,
            "source_bom_binding_identity": source_binding,
            "resolved_bom_binding_identity": resolved_binding,
        }

    def commit(
        self,
        prepared: Mapping[str, object],
        reference: str,
        dependency_resolution: Mapping[str, object],
    ) -> Mapping[str, object]:
        digest = str(prepared["tree_digest"])
        resolution, source_binding, resolved_binding = _workspace_dependency_identities(
            dependency_resolution
        )
        if (
            prepared.get("dependency_resolution_identity") != resolution
            or prepared.get("source_bom_binding_identity") != source_binding
            or prepared.get("resolved_bom_binding_identity") != resolved_binding
        ):
            raise ValueError(
                "prepared workspace dependency evidence changed before commit"
            )
        value = self._prepared.get(digest)
        if value is None:
            value = self._recover_prepared(digest)
        intelligence: SourceIntelligenceArtifact | None = None
        provider = self.source_intelligence.provider
        intelligence_status = self.source_intelligence.status(
            current=provider is not None
        )

        def require_source_intelligence(final: Path) -> Mapping[str, object]:
            nonlocal intelligence, intelligence_status, provider

            def without_intelligence(
                files: Mapping[str, bytes], *, reason: str
            ) -> Mapping[str, object]:
                nonlocal intelligence, intelligence_status, provider
                _remove_stale_source_intelligence(final, files)
                provider = None
                intelligence = None
                intelligence_status = SourceIntelligenceProviderSelection(
                    self.source_intelligence.stage,
                    self.source_intelligence.mode,
                    self.source_intelligence.provider_id,
                    None,
                    reason,
                ).status(current=False)
                return {
                    "source_intelligence_status": intelligence_status,
                    "dependency_resolution_identity": resolution,
                    "source_bom_binding_identity": source_binding,
                    "resolved_bom_binding_identity": resolved_binding,
                }

            if provider is None:
                _remove_stale_source_intelligence(
                    final, _prepared_source_files(final, value.manifest)
                )
                return {
                    "source_intelligence_status": intelligence_status,
                    "dependency_resolution_identity": resolution,
                    "source_bom_binding_identity": source_binding,
                    "resolved_bom_binding_identity": resolved_binding,
                }
            files = _prepared_source_files(final, value.manifest)
            marker = final / ".literate-source-intelligence.json"
            if marker.is_file() and not marker.is_symlink():
                try:
                    existing = json.loads(marker.read_text(encoding="utf-8"))
                    if not isinstance(existing, Mapping):
                        raise TypeError
                    intelligence = provider.verify(final, files, existing)
                except Exception:
                    _remove_stale_source_intelligence(final, files)
                else:
                    return {
                        "source_intelligence_status": intelligence_status,
                        "source_intelligence_identity": (
                            intelligence.intelligence_identity
                        ),
                        "source_intelligence_artifact_identity": (
                            intelligence.artifact_identity
                        ),
                        "dependency_resolution_identity": resolution,
                        "source_bom_binding_identity": source_binding,
                        "resolved_bom_binding_identity": resolved_binding,
                    }
            elif marker.exists() or marker.is_symlink():
                raise ValueError("workspace source-intelligence marker is unsafe")
            elif (final / ".codegraph").exists() or (final / ".codegraph").is_symlink():
                _remove_stale_index_sidecar(final)
            try:
                derived = (
                    self.source_intelligence_custody.obtain(
                        final, files, source_tree_identity=digest
                    )
                    if self.source_intelligence_custody is not None
                    else provider.index(final, files, source_tree_identity=digest)
                )
                intelligence = provider.verify(
                    final,
                    files,
                    derived,
                )
            except Exception:
                if (
                    self.source_intelligence.mode
                    is not SourceIntelligenceMode.PREFERRED
                ):
                    raise
                return without_intelligence(
                    files,
                    reason="source-intelligence.provider-execution-failed",
                )
            temporary = self.store.locks / (
                f".source-intelligence-{digest[7:]}.tmp-{os.getpid()}.json"
            )
            try:
                temporary.write_text(
                    json.dumps(
                        intelligence.to_dict(), sort_keys=True, separators=(",", ":")
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary, final / ".literate-source-intelligence.json")
            finally:
                temporary.unlink(missing_ok=True)
            return {
                "source_intelligence_status": intelligence_status,
                "source_intelligence_identity": intelligence.intelligence_identity,
                "source_intelligence_artifact_identity": intelligence.artifact_identity,
                "dependency_resolution_identity": resolution,
                "source_bom_binding_identity": source_binding,
                "resolved_bom_binding_identity": resolved_binding,
            }

        final = self.store.commit(
            value,
            reference,
            pre_reference=require_source_intelligence,
        )
        if provider is not None and intelligence is None:
            raise ValueError(
                "workspace publication omitted mandatory source intelligence"
            )
        self._prepared.pop(digest, None)
        return {
            "tree_digest": digest,
            "reference": reference,
            "path": str(final),
            "source_intelligence": (
                None if intelligence is None else intelligence.to_dict()
            ),
            "source_intelligence_status": intelligence_status,
            "dependency_resolution_identity": resolution,
            "source_bom_binding_identity": source_binding,
            "resolved_bom_binding_identity": resolved_binding,
        }

    def recover(self) -> Sequence[Mapping[str, object]]:
        return tuple({"staging_path": str(path)} for path in self.store.recover())

    def _recover_prepared(self, digest: str) -> PreparedTree:
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in digest[7:])
        ):
            raise ValueError("prepared workspace tree identity is invalid")
        for path in self.store.recover():
            metadata_path = path / ".literate-tree.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                manifest = tuple(
                    (str(item[0]), str(item[1])) for item in metadata["files"]
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if metadata.get("tree_digest") == digest:
                return PreparedTree(digest, path, manifest)
        final = self.store.trees / digest.removeprefix("sha256:")
        metadata_path = final / ".literate-tree.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                manifest = tuple(
                    (str(item[0]), str(item[1])) for item in metadata["files"]
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
            else:
                if metadata.get("tree_digest") == digest:
                    return PreparedTree(
                        digest,
                        self.store.staging / f".recovered-{digest[7:]}",
                        manifest,
                    )
        raise ValueError("prepared workspace tree is unavailable")


def _workspace_dependency_identities(
    result: Mapping[str, object],
) -> tuple[str, str, str]:
    resolution = result.get("resolution_identity")
    source_value = result.get("source_bom")
    resolved_value = result.get("resolved_bom")
    if (
        not isinstance(resolution, str)
        or not isinstance(source_value, Mapping)
        or not isinstance(resolved_value, Mapping)
    ):
        raise ValueError("workspace requires exact dependency-resolution evidence")
    source = CycloneDxBomBinding.from_dict(source_value)
    resolved = CycloneDxBomBinding.from_dict(resolved_value)
    return resolution, source.identity.uri, resolved.identity.uri


def _prepared_source_files(
    root: Path, manifest: Sequence[tuple[str, str]]
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name, expected_digest in manifest:
        relative = canonical_relative_posix_path(name, label="workspace source path")
        path = contained_materialization_target(
            root, relative, label="workspace source path"
        )
        require_materialized_file_contained(root, path, label="workspace source path")
        content = path.read_bytes()
        actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if actual_digest != expected_digest:
            raise ValueError(
                "workspace source changed before source-intelligence indexing"
            )
        files[relative.as_posix()] = content
    return files


def _remove_stale_index_sidecar(root: Path) -> None:
    sidecar = root / ".codegraph"
    if sidecar.is_symlink() or not sidecar.is_dir():
        raise ValueError("workspace reserved index sidecar is unsafe")
    allowed = {
        ".gitignore",
        "codegraph.db",
        "codegraph.db-shm",
        "codegraph.db-wal",
    }
    paths = tuple(sidecar.iterdir())
    if any(
        path.name not in allowed or path.is_symlink() or not path.is_file()
        for path in paths
    ):
        raise ValueError("workspace reserved index sidecar contains unsafe artifacts")
    for path in paths:
        path.chmod(0o600)
        path.unlink()
    sidecar.chmod(0o700)
    sidecar.rmdir()


def _remove_stale_source_intelligence(
    root: Path, source_files: Mapping[str, bytes]
) -> None:
    markers = tuple(
        path
        for path in (
            root / ".literate-source-intelligence.json",
            root / ".literate-source-index.json",
        )
        if path.exists() or path.is_symlink()
    )
    if len(markers) > 1:
        raise ValueError("workspace has conflicting source-intelligence markers")
    artifact_root: Path | None = None
    if markers:
        marker = markers[0]
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("workspace source-intelligence marker is unsafe")
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
            evidence = SourceIntelligenceArtifact.from_dict(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "workspace source-intelligence marker is malformed"
            ) from exc
        top_level = evidence.artifact_path.split("/", 1)[0]
        if top_level in {path.split("/", 1)[0] for path in source_files}:
            raise ValueError("workspace source-intelligence artifact overlaps source")
        artifact_root = root / top_level
    elif (root / ".codegraph").exists() or (root / ".codegraph").is_symlink():
        artifact_root = root / ".codegraph"
    if artifact_root is not None:
        if artifact_root == root / ".codegraph":
            _remove_stale_index_sidecar(root)
        elif artifact_root.is_symlink():
            raise ValueError("workspace source-intelligence artifact is unsafe")
        elif artifact_root.is_dir():
            paths = tuple(artifact_root.rglob("*"))
            if any(path.is_symlink() for path in paths):
                raise ValueError("workspace source-intelligence artifact is unsafe")
            for path in sorted(paths, key=lambda value: len(value.parts), reverse=True):
                if path.is_dir():
                    path.chmod(0o700)
                    path.rmdir()
                elif path.is_file():
                    path.chmod(0o600)
                    path.unlink()
                else:
                    raise ValueError("workspace source-intelligence artifact is unsafe")
            artifact_root.chmod(0o700)
            artifact_root.rmdir()
        elif artifact_root.exists():
            if not artifact_root.is_file():
                raise ValueError("workspace source-intelligence artifact is unsafe")
            artifact_root.chmod(0o600)
            artifact_root.unlink()
    for marker in markers:
        marker.chmod(0o600)
        marker.unlink()


@dataclass(slots=True)
class LifecycleEventStoreAdapter:
    """Preserve orchestration events inside the hash-chained event store."""

    store: AppendOnlyEventStore

    def append(self, stream_id: str, event: Mapping[str, object]) -> None:
        self.store.append(stream_id, str(event["event_type"]), event)

    def stream(self, stream_id: str) -> Sequence[Mapping[str, object]]:
        return tuple(item.data for item in self.store.read(stream_id))


__all__ = [
    "CppBuildAdapter",
    "GeneratedTreeSecurityClassifier",
    "JavaScriptBuildAdapter",
    "LifecycleEventStoreAdapter",
    "PipelineValidatorAdapter",
    "PolicyBuildAuthorizer",
    "PythonBuildAdapter",
    "RustBuildAdapter",
    "RustJavaScriptBuildAdapter",
    "WorkspaceTreeAdapter",
]
