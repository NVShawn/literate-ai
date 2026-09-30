"""Cargo-native artifact production for the local Standard lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from literate_ai.adapters.builders import BuildError, run_bounded_process
from literate_ai.adapters.dependencies import HostDependencyObservation
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
from literate_ai.storage import canonical_json_bytes

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


class StandardCargoLifecyclePorts(LocalStandardLifecyclePorts):
    """Local Standard ports that derive Cargo resolution after authorization."""

    def __init__(
        self,
        *,
        source_trees: LocalSourceTreeRegistry,
        object_root: Path,
        contracts: tuple[ComponentCommandContract, ...],
        tool_bindings: tuple[LocalComponentToolBinding, ...] = (),
        cargo_targets: tuple[StandardCargoTarget, ...],
        npm_targets: tuple[StandardNpmTarget, ...] = (),
        python_targets: tuple[StandardPythonTarget, ...] = (),
        python_wheelhouse: Path | None = None,
        provider_environment: Mapping[str, tuple[str, str]] | None = None,
        dependency_observation: HostDependencyObservation | None = None,
        independent_acceptance_oracle: object | None = None,
        browser_driver: object | None = None,
        native_sdk_inputs: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(
            source_trees=source_trees,
            object_root=object_root,
            contracts=contracts,
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

    def _allow_missing_cargo_lock(self, plan: StandardComponentBuildPlan) -> bool:
        return plan.component_revision.uri in self.cargo_targets

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
    ) -> dict[str, object]:
        evidence_root = artifact_root / ".literate" / "cargo"
        if evidence_root.exists() or evidence_root.is_symlink():
            raise LocalStandardLifecycleError(
                "build output occupied the reserved Cargo evidence path"
            )
        evidence_root.mkdir(parents=True)
        (evidence_root / "Cargo.lock").write_bytes(lock_bytes)
        (evidence_root / "metadata.json").write_bytes(metadata_bytes)
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
        return self._resolve_and_write_sbom(
            plan,
            artifact_root,
            artifact_digest=manifest_digest,
            additional_build_evidence=additional,
        )

    def _build_locked(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
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
        cache_identity = canonical_identity(
            {
                "schema": "literate-ai/local-standard-cargo-cache-key@1",
                "command_contract_identity": contract.identity.uri,
                "cargo_target_identity": target.identity.uri,
                "build_plan_identity": plan.identity.uri,
                "source_tree_identity": plan.request.source_tree_identity.uri,
                "provider_materials": provider_materials,
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

        staging = Path(tempfile.mkdtemp(prefix="standard-cargo-", dir=self.object_root))
        projection = staging / "projection"
        object_workspace = staging / "objects"
        artifact_workspace = staging / "artifact"
        try:
            shutil.copytree(source, projection)
            projected_manifest = projection / Path(
                *PurePosixPath(target.manifest).parts
            )
            projected_lock = projected_manifest.with_name("Cargo.lock")
            object_workspace.mkdir()
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

            def run(arguments: tuple[str, ...]):
                binding.require_unchanged()
                environment = self._binding_environment(
                    self._environment(provider_artifacts), cargo_binding
                )
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
                    raise LocalStandardLifecycleError(str(exc)) from exc
                finally:
                    binding.require_unchanged()
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
            build = run(
                (
                    "build",
                    "--locked",
                    "--manifest-path",
                    str(projected_manifest),
                    *build_target,
                )
            )
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
            shutil.rmtree(staging)
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
