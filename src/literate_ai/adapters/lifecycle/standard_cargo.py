"""Cargo-native artifact production for the local Standard lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import tomllib
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from literate_ai._cache_lock import exclusive_cache_lock
from literate_ai._filesystem import path_is_link_or_reparse, require_safe_directory
from literate_ai.adapters.builders import BuildError, run_bounded_process
from literate_ai.adapters.builders.rust import RustToolchain
from literate_ai.adapters.compiler_cache import compiler_cache_session
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.shared_cache_config import BoundSharedCache
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildOutput,
    StandardComponentBuildPlan,
)
from literate_ai.contracts import (
    ArtifactExport,
    ComponentCommandContract,
    ComponentCommandPhase,
    ContentIdentity,
    CycloneDxBomBinding,
    canonical_identity,
)
from literate_ai.diagnostics import redact_secrets
from literate_ai.storage import canonical_json_bytes

from .cargo_workspace import sync_projection, workspace_digest
from .native_cargo_inputs import (
    bind_native_cargo_inputs,
    validate_native_cargo_provider_locks,
)
from .native_cargo_projection import (
    MAX_PROJECTION_FILES,
    project_native_cargo_graph,
    validate_native_cargo_metadata,
)
from .standard_local import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    local_generated_source_tree_identity,
    local_tree_identity,
)
from .standard_npm import StandardNpmTarget
from .standard_python import StandardPythonTarget

_CARGO_EVIDENCE_MANIFEST = ".literate/cargo/evidence-manifest.json"
_MAX_CARGO_EVIDENCE_BYTES = 16 * 1024 * 1024
_CARGO_METADATA_ID = re.compile(r"^[^\s\r\n]+(?:\s+[^\r\n]+)?$")


@dataclass(frozen=True, slots=True)
class StandardCargoTarget:
    """Exact Cargo package target selected for one authorized Component build."""

    component_revision: ContentIdentity
    build_system_resolver_identity: ContentIdentity
    build_system_toolchain_identity: ContentIdentity
    language_compiler_identity: ContentIdentity
    manifest: str
    binary: str
    rustc_command: tuple[str, ...]
    library: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.component_revision, "Component revision"),
            (self.build_system_resolver_identity, "resolver"),
            (self.build_system_toolchain_identity, "Cargo toolchain"),
            (self.language_compiler_identity, "Rust compiler"),
        ):
            if not isinstance(value, ContentIdentity):
                raise TypeError(f"Cargo target {label} must be an identity")
        path = PurePosixPath(self.manifest)
        if (
            path.is_absolute()
            or path.name != "Cargo.toml"
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != self.manifest
        ):
            raise ValueError("Cargo manifest must be a canonical relative Cargo.toml")
        if (
            not self.binary
            or not self.binary[0].isalnum()
            or any(
                not (character.isascii() and (character.isalnum() or character in "_-"))
                for character in self.binary
            )
        ):
            raise ValueError("Cargo binary must be one portable target name")
        if not self.rustc_command or any(not item for item in self.rustc_command):
            raise ValueError("Cargo target requires one exact Rust compiler command")
        if not isinstance(self.library, bool):
            raise TypeError("Cargo target library marker must be bool")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        value = {
            "schema": "literate-ai/standard-cargo-target@1",
            "component_revision": self.component_revision.uri,
            "build_system_resolver_identity": (self.build_system_resolver_identity.uri),
            "build_system_toolchain_identity": (
                self.build_system_toolchain_identity.uri
            ),
            "language_compiler_identity": self.language_compiler_identity.uri,
            "manifest": self.manifest,
            "binary": self.binary,
            "rustc_command": list(self.rustc_command),
        }
        if self.library:
            value["library"] = True
        return value


_COMPILER_ISSUER = object()
_COMPILER_OBSERVATIONS = weakref.WeakKeyDictionary()


class _RustHarnessCompilerObservations:
    """Original PID-local measurements; no wire decoder or execution grant."""

    __slots__ = ("__weakref__",)

    def __init__(self, *, _issuer):
        if _issuer is not _COMPILER_ISSUER:
            raise TypeError("original Rust compiler observations required")

    def _originals(self):
        if type(self) is not _RustHarnessCompilerObservations:
            raise LocalStandardLifecycleError(
                "original Rust compiler observations required"
            )
        record = _COMPILER_OBSERVATIONS.get(self)
        if record is None:
            raise LocalStandardLifecycleError(
                "original Rust compiler observations required"
            )
        pid, compilers = record
        if os.getpid() != pid:
            raise LocalStandardLifecycleError(
                "Rust compiler observation process changed"
            )
        return compilers

    def require_targets(self, targets):
        expected = {
            target.language_compiler_identity.uri: target.rustc_command
            for target in targets
            if target.library
        }
        actual = {tool.identity: tool.command for tool in self._originals()}
        if actual != expected:
            raise LocalStandardLifecycleError(
                "Rust compiler observations differ from targets"
            )

    def require_compiler(self, target):
        matches = tuple(
            tool
            for tool in self._originals()
            if tool.identity == target.language_compiler_identity.uri
            and tool.command == target.rustc_command
        )
        if not target.library or len(matches) != 1:
            raise LocalStandardLifecycleError(
                "exact original Rust compiler unavailable"
            )
        return matches[0]

    def __copy__(self):
        raise TypeError("Rust compiler observations cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("Rust compiler observations cannot be copied")

    def __reduce__(self):
        raise TypeError("Rust compiler observations cannot be restored")


def _retain_rust_harness_compilers(compilers):
    if (
        type(compilers) is not tuple
        or any(type(tool) is not RustToolchain for tool in compilers)
        or len({tool.identity for tool in compilers}) != len(compilers)
    ):
        raise TypeError("unique original measured Rust compilers required")
    observation = _RustHarnessCompilerObservations(_issuer=_COMPILER_ISSUER)
    _COMPILER_OBSERVATIONS[observation] = (os.getpid(), compilers)
    return observation


class StandardCargoLifecyclePorts(LocalStandardLifecyclePorts):
    """Local Standard ports that derive Cargo resolution after authorization."""

    def __init__(
        self,
        *,
        source_trees: LocalSourceTreeRegistry,
        object_root: Path,
        contracts: tuple[ComponentCommandContract, ...],
        command_phases: tuple[ComponentCommandPhase, ...] = tuple(
            ComponentCommandPhase
        ),
        tool_bindings: tuple[LocalComponentToolBinding, ...] = (),
        cargo_targets: tuple[StandardCargoTarget, ...],
        rust_harness_compilers: _RustHarnessCompilerObservations | None = None,
        npm_targets: tuple[StandardNpmTarget, ...] = (),
        python_targets: tuple[StandardPythonTarget, ...] = (),
        python_wheelhouse: Path | None = None,
        provider_environment: Mapping[str, tuple[str, str]] | None = None,
        dependency_observation: HostDependencyObservation | None = None,
        independent_acceptance_oracle: object | None = None,
        browser_driver: object | None = None,
        native_sdk_inputs: object | None = None,
        shared_cache: BoundSharedCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if rust_harness_compilers is not None:
            if type(rust_harness_compilers) is not _RustHarnessCompilerObservations:
                raise TypeError("original Rust compiler observations required")
            rust_harness_compilers.require_targets(cargo_targets)
        super().__init__(
            source_trees=source_trees,
            object_root=object_root,
            contracts=contracts,
            command_phases=command_phases,
            tool_bindings=tool_bindings,
            npm_targets=npm_targets,
            python_targets=python_targets,
            python_wheelhouse=python_wheelhouse,
            provider_environment=provider_environment,
            dependency_observation=dependency_observation,
            independent_acceptance_oracle=independent_acceptance_oracle,
            browser_driver=browser_driver,
            native_sdk_inputs=native_sdk_inputs,
            clock=clock,
        )
        self.shared_cache = shared_cache
        self.cargo_targets = {
            item.component_revision.uri: item for item in cargo_targets
        }
        if len(self.cargo_targets) != len(cargo_targets):
            raise ValueError("Standard Cargo targets must name unique Components")
        self._cargo_resolution_builds: dict[str, dict[str, object]] = {}
        for revision, target in self.cargo_targets.items():
            contract = self.contracts.get(revision)
            if contract is None:
                raise ValueError("Cargo target names an unplanned Component")
            if (
                contract.locked_build_authority_identity != target.identity
                or contract.build_system_resolver_identity
                != target.build_system_resolver_identity
                or contract.build_system_toolchain_identity
                != target.build_system_toolchain_identity
                or contract.language_compiler_identity
                != target.language_compiler_identity
                or contract.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity
                != target.build_system_toolchain_identity
            ):
                raise ValueError(
                    "Standard Cargo target does not match locked build authority"
                )
        self._rust_harness_compilers = rust_harness_compilers

    def _allow_missing_cargo_lock(self, plan: StandardComponentBuildPlan) -> bool:
        return plan.component_revision.uri in self.cargo_targets

    def _current_native_cargo_inputs(self, plan, providers):
        if not any(
            self._contract(item.component_revision).is_library for item in providers
        ):
            return None
        inputs = self.build_execution_inputs(plan)
        native = bind_native_cargo_inputs(
            consumer_revision=plan.component_revision,
            target_identity=self._contract(
                plan.component_revision
            ).artifact_export.target_identity,
            providers=providers,
            available_exports=self._exports_by_identity,
            contracts=self.contracts,
            manifest_paths={
                revision: target.manifest
                for revision, target in self.cargo_targets.items()
            },
            current_bindings=self.library_consumer_bindings(inputs.intent),
            read_blob=self.read_artifact_blob,
        )
        validate_native_cargo_provider_locks(graph=native[0], sources=native[1])
        return native

    def _cargo_workspace_identity(self, plan, providers, contract, native):
        compatibility = self.cargo_targets[
            plan.component_revision.uri
        ].identity_document()
        compatibility.pop("component_revision")
        return canonical_identity(
            {
                "schema": "literate-ai/cargo-workspace@1",
                "cargo": compatibility,
                "target": contract.artifact_export.target_identity.uri,
                "abi": contract.artifact_export.abi_identity.uri,
                "providers": self._provider_materials(
                    providers, consumer_revision=plan.component_revision
                ),
                "compiler_cache": self.shared_cache.identity.uri
                if self.shared_cache
                else None,
                **(
                    {"native_provider_projection_v1": native[0].identity.uri}
                    if native
                    else {}
                ),
            }
        )

    def _native_cargo_evidence(self, plan, providers, native, metadata, lock_bytes):
        graph, sources = native
        target = self.cargo_targets[plan.component_revision.uri]
        root = self.source_trees.resolve(plan.request.source_tree_identity)
        original = self._read_bounded(
            root / target.manifest, label="original native manifest"
        )
        workspace = self._cargo_workspace_identity(
            plan, providers, self._contract(plan.component_revision), native
        )
        binding = validate_native_cargo_metadata(
            graph=graph,
            root_manifest=original,
            root_manifest_path=target.manifest,
            sources=sources,
            projection_root=self.object_root
            / "cargo-work"
            / workspace.digest
            / "projection",
            metadata=metadata,
        )
        provider_locks = validate_native_cargo_provider_locks(
            graph=graph,
            sources=sources,
            consumer_manifest=original,
            consumer_lock=lock_bytes,
        )
        return {
            "schema": "literate-ai/standard-native-cargo-evidence@1",
            "graph": graph.to_dict(),
            "original_manifest_identity": "sha256:"
            + hashlib.sha256(original).hexdigest(),
            "metadata_binding": binding,
            "provider_locks": provider_locks,
        }

    def _cargo_build_environment(self, providers):
        # Native library imports are represented by the admitted manifest graph,
        # not by an otherwise unused process-runtime environment binding.
        return self._environment(
            tuple(
                item
                for item in providers
                if not self._contract(item.component_revision).is_library
            )
        )

    @staticmethod
    def _read_bounded(path: Path, *, label: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise LocalStandardLifecycleError(f"Cargo {label} is not a regular file")
        content = path.read_bytes()
        if len(content) > _MAX_CARGO_EVIDENCE_BYTES:
            raise LocalStandardLifecycleError(
                f"Cargo {label} exceeds the evidence limit"
            )
        return content

    @staticmethod
    def _evidence_records(root: Path) -> list[dict[str, str]]:
        records = []
        for path in sorted(root.iterdir()):
            if path.name == "evidence-manifest.json":
                continue
            content = StandardCargoLifecyclePorts._read_bounded(
                path, label="dependency evidence"
            )
            records.append(
                {
                    "path": f".literate/cargo/{path.name}",
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
            )
        return records

    def _write_cargo_dependency_evidence(
        self,
        plan: StandardComponentBuildPlan,
        artifact_root: Path,
        *,
        target: StandardCargoTarget,
        lock_bytes: bytes,
        metadata_bytes: bytes,
        native_evidence: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        evidence_root = artifact_root / ".literate" / "cargo"
        if evidence_root.exists() or evidence_root.is_symlink():
            raise LocalStandardLifecycleError(
                "build output occupied the reserved Cargo evidence path"
            )
        evidence_root.mkdir(parents=True)
        (evidence_root / "Cargo.lock").write_bytes(lock_bytes)
        (evidence_root / "metadata.json").write_bytes(metadata_bytes)
        native_bytes = None
        if native_evidence is not None:
            native_bytes = canonical_json_bytes(native_evidence)
            (evidence_root / "native-provider-graph.json").write_bytes(native_bytes)
        manifest = {
            "schema": "urn:literate-ai:schema:v1:standard-cargo-dependency-evidence",
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-cargo-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "source_bundle_digest": plan.request.source_tree_identity.uri,
            "build_toolchain_identity": target.build_system_toolchain_identity.uri,
            "language_compiler_identity": target.language_compiler_identity.uri,
            "target_identity": target.identity.uri,
            "lock_identity": "sha256:" + hashlib.sha256(lock_bytes).hexdigest(),
            "metadata_identity": "sha256:" + hashlib.sha256(metadata_bytes).hexdigest(),
            "files": self._evidence_records(evidence_root),
        }
        manifest_bytes = canonical_json_bytes(manifest)
        (evidence_root / "evidence-manifest.json").write_bytes(manifest_bytes)
        if self._evidence_recorder is not None:
            for payload in (lock_bytes, metadata_bytes, manifest_bytes):
                self._evidence_recorder.remember_bytes(payload)
            if native_bytes is not None:
                self._evidence_recorder.remember_bytes(native_bytes)
            self._record_evidence(target.identity_document())
        return {
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-cargo-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "cargo_lock": {
                "path": str(PurePosixPath(target.manifest).parent / "Cargo.lock"),
                "content": lock_bytes.decode("utf-8"),
                "identity": manifest["lock_identity"],
            },
            "cargo_metadata_identity": manifest["metadata_identity"],
            "standard_cargo_evidence_manifest": _CARGO_EVIDENCE_MANIFEST,
            "evidence_manifest_digest": "sha256:"
            + hashlib.sha256(manifest_bytes).hexdigest(),
        }

    def _validated_cargo_evidence(
        self, plan: StandardComponentBuildPlan, artifact_root: Path
    ) -> dict[str, object]:
        evidence = self._cargo_resolution_builds.get(plan.identity.uri)
        manifest_path = artifact_root.joinpath(*Path(_CARGO_EVIDENCE_MANIFEST).parts)
        manifest_bytes = self._read_bounded(manifest_path, label="evidence manifest")
        actual_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        expected_digest = (
            actual_digest
            if evidence is None
            else evidence.get("evidence_manifest_digest")
        )
        if expected_digest != actual_digest:
            raise LocalStandardLifecycleError(
                "Standard Cargo dependency evidence manifest changed"
            )
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalStandardLifecycleError(
                "Standard Cargo dependency evidence manifest is invalid"
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("authorization_id")
            != plan.request.authorization_identity.uri
            or manifest.get("component_revision") != plan.component_revision.uri
            or manifest.get("build_plan_identity") != plan.identity.uri
            or manifest.get("source_bundle_digest")
            != plan.request.source_tree_identity.uri
        ):
            raise LocalStandardLifecycleError(
                "Standard Cargo dependency evidence names different authority"
            )
        expected_files = manifest.get("files")
        actual_files = self._evidence_records(manifest_path.parent)
        if expected_files != actual_files:
            raise LocalStandardLifecycleError(
                "Standard Cargo dependency manifest does not cover its exact files"
            )
        lock_bytes = self._read_bounded(
            manifest_path.parent / "Cargo.lock", label="retained lock"
        )
        metadata_bytes = self._read_bounded(
            manifest_path.parent / "metadata.json", label="retained metadata"
        )
        if (
            manifest.get("lock_identity")
            != "sha256:" + hashlib.sha256(lock_bytes).hexdigest()
            or manifest.get("metadata_identity")
            != "sha256:" + hashlib.sha256(metadata_bytes).hexdigest()
        ):
            raise LocalStandardLifecycleError(
                "Standard Cargo dependency evidence digest is inconsistent"
            )
        try:
            metadata = json.loads(metadata_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalStandardLifecycleError(
                "retained Cargo metadata is invalid"
            ) from exc
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("packages"), list
        ):
            raise LocalStandardLifecycleError("retained Cargo metadata root is invalid")
        try:
            lock_document = tomllib.loads(lock_bytes.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise LocalStandardLifecycleError("retained Cargo lock is invalid") from exc
        lock_packages = lock_document.get("package")
        metadata_packages = metadata["packages"]
        if not isinstance(lock_packages, list) or {
            (package.get("name"), package.get("version"))
            for package in lock_packages
            if isinstance(package, dict)
        } != {
            (package.get("name"), package.get("version"))
            for package in metadata_packages
            if isinstance(package, dict)
        }:
            raise LocalStandardLifecycleError(
                "Cargo metadata package graph differs from the derived lock"
            )
        cargo_lock = None if evidence is None else evidence.get("cargo_lock")
        if evidence is not None and (
            not isinstance(cargo_lock, dict)
            or cargo_lock.get("content") != lock_bytes.decode("utf-8")
        ):
            raise LocalStandardLifecycleError(
                "retained Cargo lock differs from resolved dependency evidence"
            )
        target = self.cargo_targets[plan.component_revision.uri]
        providers = tuple(
            self._exports_by_identity[identity.uri]
            for identity in plan.provider_artifact_identities
        )
        native = self._current_native_cargo_inputs(plan, providers)
        native_path = manifest_path.parent / "native-provider-graph.json"
        if native is not None:
            expected_native = self._native_cargo_evidence(
                plan, providers, native, metadata, lock_bytes
            )
            if self._read_bounded(
                native_path, label="native provider evidence"
            ) != canonical_json_bytes(expected_native):
                raise LocalStandardLifecycleError(
                    "retained native Cargo graph differs from current qualified inputs"
                )
        elif native_path.exists() or native_path.is_symlink():
            raise LocalStandardLifecycleError(
                "unexpected native Cargo provider evidence"
            )
        return {
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-cargo-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "cargo_lock": {
                "path": str(PurePosixPath(target.manifest).parent / "Cargo.lock"),
                "content": lock_bytes.decode("utf-8"),
                "identity": manifest["lock_identity"],
            },
            "cargo_metadata_identity": manifest["metadata_identity"],
            "standard_cargo_evidence_manifest": _CARGO_EVIDENCE_MANIFEST,
            "evidence_manifest_digest": actual_digest,
        }

    def _write_resolved_sbom(
        self, plan: StandardComponentBuildPlan, artifact_root: Path
    ) -> CycloneDxBomBinding:
        if plan.component_revision.uri not in self.cargo_targets:
            return super()._write_resolved_sbom(plan, artifact_root)
        evidence = self._validated_cargo_evidence(plan, artifact_root)
        manifest_digest = evidence["evidence_manifest_digest"]
        assert isinstance(manifest_digest, str)
        additional = {
            key: value
            for key, value in evidence.items()
            if key != "evidence_manifest_digest"
        }
        additional["cargo_lifecycle_profile"] = True
        providers = tuple(
            self._exports_by_identity[identity.uri]
            for identity in plan.provider_artifact_identities
        )
        native = self._current_native_cargo_inputs(plan, providers)
        return self._resolve_and_write_sbom(
            plan,
            artifact_root,
            artifact_digest=manifest_digest,
            additional_build_evidence=additional,
            native_cargo_graph=native[0] if native else None,
            native_cargo_manifest=self.cargo_targets[
                plan.component_revision.uri
            ].manifest
            if native
            else None,
        )

    def _build_locked(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
    ) -> StandardBuildOutput:
        target = self.cargo_targets.get(plan.component_revision.uri)
        if target is None:
            return self._build_cargo_locked(plan, provider_artifacts, contract)
        # Without native providers, Cargo invalidates changed consumer inputs.
        # Native graph identity also includes the consumer revision, so native
        # workspaces remain conservatively partitioned by that revision.
        native = self._current_native_cargo_inputs(plan, provider_artifacts)
        workspace_key = self._cargo_workspace_identity(
            plan, provider_artifacts, contract, native
        )
        with exclusive_cache_lock(
            self.object_root / ".cargo-locks" / workspace_key.digest
        ):
            stable = self.object_root / "cargo-work" / workspace_key.digest
            seal = stable.with_suffix(".seal")
            require_safe_directory(stable, allow_missing=True)
            if path_is_link_or_reparse(seal):
                raise LocalStandardLifecycleError("Cargo workspace seal is unsafe")
            if stable.exists() and seal.exists():
                previous = self._read_bounded(seal, label="workspace seal")
                if previous.decode("utf-8") != workspace_digest(stable):
                    raise LocalStandardLifecycleError(
                        "Cargo workspace changed since build"
                    )
            elif stable.exists():
                # Interrupted work has no successful-build seal.
                workspace_digest(stable)  # Refuse unsafe nodes before cleanup.
                shutil.rmtree(stable)
            # An interrupted process must not leave a successful-build marker
            # attached to partially updated inputs or compiler output.
            seal.unlink(missing_ok=True)
            try:
                result = self._build_cargo_locked(
                    plan, provider_artifacts, contract, stable_staging=stable
                )
                if stable.exists():
                    seal.write_text(workspace_digest(stable), encoding="utf-8")
                return result
            except Exception:
                if stable.exists():
                    shutil.rmtree(stable)
                seal.unlink(missing_ok=True)
                raise

    def _build_cargo_locked(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
        *,
        stable_staging: Path | None = None,
    ) -> StandardBuildOutput:
        target = self.cargo_targets.get(plan.component_revision.uri)
        if target is None:
            return super()._build_locked(plan, provider_artifacts, contract)
        declaration = plan.manifest.export_declarations[0]
        shape = contract.artifact_export
        if (
            declaration.export_id,
            declaration.component_revision,
            declaration.role,
            declaration.abi_identity,
            declaration.target_identity,
            declaration.media_type,
            declaration.producer_identity,
            declaration.toolchain_identity,
        ) != (
            shape.export_id,
            contract.component_revision,
            shape.role,
            shape.abi_identity,
            shape.target_identity,
            shape.media_type,
            shape.producer_identity,
            contract.language_compiler_identity,
        ):
            raise LocalStandardLifecycleError(
                "build plan does not realize the exact locked Cargo export shape"
            )
        if tuple(item.identity for item in provider_artifacts) != (
            plan.provider_artifact_identities
        ):
            raise LocalStandardLifecycleError(
                "Cargo builder received different provider artifacts"
            )

        source = self.source_trees.resolve(plan.request.source_tree_identity)
        source_identity = local_generated_source_tree_identity(source)
        source_manifest = source / Path(*PurePosixPath(target.manifest).parts)
        source_lock = source_manifest.with_name("Cargo.lock")
        if source_lock.exists() or source_lock.is_symlink():
            raise LocalStandardLifecycleError(
                "generated Cargo source must not contain Cargo.lock"
            )
        if source_manifest.is_symlink() or not source_manifest.is_file():
            raise LocalStandardLifecycleError(
                "generated Cargo source requires its locked Cargo.toml path"
            )
        provider_materials = self._provider_materials(
            provider_artifacts, consumer_revision=plan.component_revision
        )
        native = self._current_native_cargo_inputs(plan, provider_artifacts)
        cache_identity = canonical_identity(
            {
                "schema": "literate-ai/local-standard-cargo-cache-key@1",
                "command_contract_identity": contract.identity.uri,
                "cargo_target_identity": target.identity.uri,
                "build_plan_identity": plan.identity.uri,
                "source_tree_identity": plan.request.source_tree_identity.uri,
                "provider_materials": provider_materials,
                **(
                    {"native_provider_projection_v1": native[0].identity.uri}
                    if native
                    else {}
                ),
                **(
                    {"compiler_cache_identity": self.shared_cache.identity.uri}
                    if self.shared_cache is not None
                    else {}
                ),
            }
        )
        artifact = self.object_root / cache_identity.digest
        started = time.monotonic()
        if artifact.exists():
            reused = self._reuse_cached_artifact(
                plan,
                provider_artifacts,
                provider_materials,
                cache_identity,
                artifact,
                extra_validate=lambda: self._validated_cargo_evidence(plan, artifact),
            )
            if reused is not None:
                self.build_cache_hits += 1
                self.build_cache_hit_seconds += time.monotonic() - started
                return reused

        if stable_staging is None:
            raise LocalStandardLifecycleError("Cargo build requires a locked workspace")
        staging = stable_staging
        staging.mkdir(parents=True, exist_ok=True)
        projection = staging / "projection"
        object_workspace = staging / "objects"
        artifact_workspace = staging / "artifact"
        try:
            sync_projection(source, projection)
            projected_manifest = projection / Path(
                *PurePosixPath(target.manifest).parts
            )
            projected_lock = projected_manifest.with_name("Cargo.lock")
            if native is not None:
                # Only the disposable tree receives framework-owned path edges.
                # Existing source cannot preoccupy the reserved projection namespace.
                count = 0
                for path in projection.rglob("*"):
                    count += 1
                    if count > MAX_PROJECTION_FILES:
                        raise LocalStandardLifecycleError(
                            "native Cargo source exceeds its file bound"
                        )
                    relative = path.relative_to(projection).as_posix().casefold()
                    if (
                        ".cargo" in relative.split("/")
                        or relative.startswith(".literate/native-cargo")
                        or relative
                        == str(
                            PurePosixPath(target.manifest).with_name("build.rs")
                        ).casefold()
                    ):
                        raise LocalStandardLifecycleError(
                            "generated native Cargo source contains "
                            "ambient or reserved authority"
                        )
                rewritten, files = project_native_cargo_graph(
                    graph=native[0],
                    root_manifest=self._read_bounded(
                        source_manifest, label="original native manifest"
                    ),
                    root_manifest_path=target.manifest,
                    sources=native[1],
                )
                projected_manifest.write_bytes(rewritten)
                for row in files:
                    destination = projection / row.path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(row.content)
                    destination.chmod(row.mode)
            object_workspace.mkdir(exist_ok=True)
            artifact_workspace.mkdir()
            binding_identity = contract.tool_binding(
                ComponentCommandPhase.BUILD
            ).toolchain_identity
            binding = self.tool_bindings[binding_identity.uri]
            binding.require_unchanged()
            cargo_binding = LocalComponentToolBinding(
                binding.executable,
                binding.arguments,
                binding.authority_identity,
                (
                    (
                        "CARGO_TARGET_DIR",
                        str((object_workspace / "cargo-target").resolve()),
                    ),
                    ("RUSTC", target.rustc_command[0]),
                ),
                binding._authority_guard,
            )

            def run(arguments: tuple[str, ...], *, cache_environment=None):
                binding.require_unchanged()
                environment = self._binding_environment(
                    self._cargo_build_environment(provider_artifacts), cargo_binding
                )
                if cache_environment is not None:
                    environment = dict(cache_environment)
                try:
                    result = run_bounded_process(
                        (*binding.command, *arguments),
                        cwd=projected_manifest.parent,
                        environment=environment,
                        timeout_seconds=900,
                        stdout_limit_bytes=_MAX_CARGO_EVIDENCE_BYTES,
                        stderr_limit_bytes=_MAX_CARGO_EVIDENCE_BYTES,
                        error_prefix="builder.cargo",
                    )
                except BuildError as exc:
                    raise LocalStandardLifecycleError(
                        redact_secrets(str(exc), environment)
                    ) from exc
                finally:
                    binding.require_unchanged()
                if self.shared_cache is not None:
                    token = self.shared_cache.credential()
                    if token is not None:
                        result = replace(
                            result,
                            stdout=result.stdout.replace(token.encode(), b"<redacted>"),
                            stderr=result.stderr.replace(token.encode(), b"<redacted>"),
                        )
                if result.returncode != 0:
                    raise LocalStandardLifecycleError(
                        f"Cargo command failed ({result.returncode}): "
                        f"{result.stderr.decode('utf-8', errors='replace').strip()}"
                    )
                return result

            generate = run(
                ("generate-lockfile", "--manifest-path", str(projected_manifest))
            )
            lock_bytes = self._read_bounded(projected_lock, label="generated lock")
            metadata = run(
                (
                    "metadata",
                    "--locked",
                    "--format-version=1",
                    "--manifest-path",
                    str(projected_manifest),
                )
            )
            try:
                metadata_document = json.loads(metadata.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LocalStandardLifecycleError(
                    "Cargo metadata is not valid JSON"
                ) from exc
            if not isinstance(metadata_document, dict):
                raise LocalStandardLifecycleError("Cargo metadata root is invalid")
            packages = metadata_document.get("packages")
            if not isinstance(packages, list):
                raise LocalStandardLifecycleError("Cargo metadata omits packages")
            for package in packages:
                if (
                    not isinstance(package, dict)
                    or not isinstance(package.get("name"), str)
                    or not isinstance(package.get("version"), str)
                    or (
                        isinstance(package.get("id"), str)
                        and _CARGO_METADATA_ID.fullmatch(package["id"]) is None
                    )
                ):
                    raise LocalStandardLifecycleError(
                        "Cargo metadata contains an invalid package"
                    )
            metadata_bytes = canonical_json_bytes(metadata_document)
            native_evidence = (
                self._native_cargo_evidence(
                    plan, provider_artifacts, native, metadata_document, lock_bytes
                )
                if native
                else None
            )
            try:
                lock_document = tomllib.loads(lock_bytes.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise LocalStandardLifecycleError(
                    "generated Cargo.lock is invalid"
                ) from exc
            lock_packages = lock_document.get("package")
            if not isinstance(lock_packages, list) or {
                (package.get("name"), package.get("version"))
                for package in lock_packages
                if isinstance(package, dict)
            } != {
                (package.get("name"), package.get("version"))
                for package in packages
                if isinstance(package, dict)
            }:
                raise LocalStandardLifecycleError(
                    "Cargo metadata package graph differs from the derived lock"
                )
            if self._read_bounded(projected_lock, label="validated lock") != lock_bytes:
                raise LocalStandardLifecycleError(
                    "cargo metadata --locked changed the derived Cargo.lock"
                )
            build_target = ("--lib",) if target.library else ("--bin", target.binary)
            build_arguments = (
                "build",
                "--locked",
                "--manifest-path",
                str(projected_manifest),
                *build_target,
            )
            cache_observation = None
            if (
                self.shared_cache is not None
                and self.shared_cache.compiler_tool is not None
            ):
                with compiler_cache_session(
                    self.shared_cache,
                    environment=self._binding_environment(
                        self._cargo_build_environment(provider_artifacts), cargo_binding
                    ),
                    workspace=projected_manifest.parent,
                ) as cache_session:
                    build = run(
                        build_arguments, cache_environment=cache_session.environment
                    )
                cache_observation = cache_session.observation
            else:
                build = run(build_arguments)
            if self._read_bounded(projected_lock, label="built lock") != lock_bytes:
                raise LocalStandardLifecycleError(
                    "cargo build --locked changed the derived Cargo.lock"
                )
            export_path = artifact_workspace / contract.artifact_export.export_id
            if target.library:
                shutil.copytree(projection, export_path)
            else:
                suffix = ".exe" if os.name == "nt" else ""
                produced = (
                    object_workspace
                    / "cargo-target"
                    / "debug"
                    / (target.binary + suffix)
                )
                if produced.is_symlink() or not produced.is_file():
                    raise LocalStandardLifecycleError(
                        "Cargo did not produce its declared binary"
                    )
                shutil.copy2(produced, export_path)
            if local_generated_source_tree_identity(source) != source_identity:
                raise LocalStandardLifecycleError(
                    "Cargo build mutated the admitted generated source tree"
                )
            evidence = self._write_cargo_dependency_evidence(
                plan,
                artifact_workspace,
                target=target,
                lock_bytes=lock_bytes,
                metadata_bytes=metadata_bytes,
                native_evidence=native_evidence,
            )

            def process_identity(result, phase: str) -> str:
                if self._evidence_recorder is not None:
                    self._evidence_recorder.remember_bytes(result.stdout)
                    self._evidence_recorder.remember_bytes(result.stderr)
                return self._record_evidence(
                    {
                        "schema": "literate-ai/local-cargo-process-observation@1",
                        "phase": phase,
                        "plan_identity": plan.identity.uri,
                        "returncode": result.returncode,
                        "stdout_identity": "sha256:"
                        + hashlib.sha256(result.stdout).hexdigest(),
                        "stderr_identity": "sha256:"
                        + hashlib.sha256(result.stderr).hexdigest(),
                    }
                ).uri

            process_observation = self._record_evidence(
                {
                    "schema": "literate-ai/local-cargo-build-observation@1",
                    "generate_lockfile": process_identity(
                        generate, "cargo-generate-lockfile"
                    ),
                    "metadata": process_identity(metadata, "cargo-metadata"),
                    "build": process_identity(build, "cargo-build"),
                    "target_identity": target.identity.uri,
                    **(
                        {"compiler_cache": cache_observation}
                        if cache_observation is not None
                        else {}
                    ),
                }
            )
            self._cargo_resolution_builds[plan.identity.uri] = evidence
            try:
                self._write_artifact_manifest(
                    plan, artifact_workspace, provider_materials, process_observation
                )
            finally:
                self._cargo_resolution_builds.pop(plan.identity.uri, None)
            self._install_sealed_artifact(
                cache_identity,
                artifact,
                artifact_workspace,
                conflict="Cargo-addressed local artifact has conflicting bytes",
            )
            if artifact_workspace.exists():
                shutil.rmtree(artifact_workspace)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        sealed = local_tree_identity(artifact)
        output = self._build_output(plan, artifact)
        if local_tree_identity(artifact) != sealed:
            raise LocalStandardLifecycleError(
                "Cargo-addressed local artifact changed before checkpoint publication"
            )
        self._publish_artifact_checkpoint(cache_identity, plan, artifact)
        self.build_cache_misses += 1
        self.build_seconds += time.monotonic() - started
        return output


__all__ = ["StandardCargoLifecyclePorts", "StandardCargoTarget"]
