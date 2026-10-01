"""Bazel-native artifact production for the local Standard lifecycle.

The generic local command adapter deliberately treats a build command as one argv
vector that writes ``{export_path}``.  Bazel has a different, useful contract: a
target produces declared files in Bazel's output tree.  This adapter preserves that
semantic boundary instead of compiling natively after a Bazel conformance build.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from literate_ai.adapters.builders.bazel import (
    DEFAULT_BAZEL_COMMAND_TIMEOUT_SECONDS,
    BazelBuildInputEvidence,
    BazelDependencyEvidence,
    _remove_bazel_directory,
    collect_bazel_build_input_evidence,
    collect_bzlmod_dependency_evidence,
)
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
    CppLibraryLayout,
    CycloneDxBomBinding,
    canonical_identity,
)
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.ports import BuildDependencyObservation
from literate_ai.storage import canonical_json_bytes

from .standard_local import (
    LocalComponentToolBinding,
    LocalIndependentAcceptanceOracle,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    local_generated_source_tree_identity,
    local_tree_identity,
)
from .standard_npm import StandardNpmTarget
from .standard_python import StandardPythonTarget

_BAZEL_NAME_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-"
)
_BAZEL_EVIDENCE_MANIFEST = ".literate/bazel/evidence-manifest.json"
_WINDOWS_FRESH_OUTPUT_COPY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2)


def _canonical_local_label(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("//") or "\x00" in value:
        return False
    body = value[2:]
    if not body or body.count(":") > 1:
        return False
    package, separator, name = body.partition(":")
    if separator and (
        not name or any(item not in _BAZEL_NAME_CHARACTERS for item in name)
    ):
        return False
    if package:
        parts = package.split("/")
        if any(
            not part
            or part in {".", ".."}
            or any(item not in _BAZEL_NAME_CHARACTERS for item in part)
            for part in parts
        ):
            return False
    return bool(separator or package)


@dataclass(frozen=True, slots=True)
class StandardBazelTarget:
    """Exact locked Bazel target and output selected for one Component build.

    ``output_path`` is relative to the directory reported by ``bazel info
    bazel-bin``.  Framework-owned output-root and symlink-suppression arguments are
    intentionally not author-controlled; all remaining Bazel build options are.
    """

    component_revision: ContentIdentity
    build_system_resolver_identity: ContentIdentity
    build_system_toolchain_identity: ContentIdentity
    target_label: str
    output_path: str
    build_options: tuple[str, ...] = ()
    cpp_layout: CppLibraryLayout | None = None
    cpp_test_output: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.component_revision, ContentIdentity):
            raise TypeError("Bazel target Component revision must be an identity")
        if not isinstance(self.build_system_resolver_identity, ContentIdentity):
            raise TypeError("Bazel target resolver must be an identity")
        if not isinstance(self.build_system_toolchain_identity, ContentIdentity):
            raise TypeError("Bazel target toolchain must be an identity")
        if not _canonical_local_label(self.target_label):
            raise ValueError("Bazel target label must be one canonical local label")
        if not isinstance(self.output_path, str) or not self.output_path:
            raise ValueError("Bazel target output path must be nonempty")
        output = PurePosixPath(self.output_path)
        if (
            output.is_absolute()
            or not output.parts
            or any(part in {"", ".", ".."} for part in output.parts)
            or output.as_posix() != self.output_path
        ):
            raise ValueError("Bazel target output must be a canonical relative path")
        if not isinstance(self.build_options, tuple) or any(
            not isinstance(item, str)
            or not item.startswith("--")
            or not item.strip()
            or "\x00" in item
            for item in self.build_options
        ):
            raise ValueError("Bazel build options must be exact --option argv tokens")
        reserved = ("--output_base", "--symlink_prefix")
        if any(
            option == name or option.startswith(f"{name}=")
            for option in self.build_options
            for name in reserved
        ):
            raise ValueError("Bazel output custody options are framework-owned")

        if (self.cpp_layout is None) != (self.cpp_test_output is None):
            raise ValueError("C++ Bazel products require both a layout and test output")
        if self.cpp_layout is not None:
            if not isinstance(self.cpp_layout, CppLibraryLayout):
                raise TypeError("C++ Bazel layout must be typed")
            if not isinstance(
                self.cpp_test_output, str
            ) or not self.cpp_test_output.startswith("tests/"):
                raise ValueError("C++ generated test output must be below tests/")
            canonical_relative_posix_paths(
                (*self.cpp_layout.files, self.cpp_test_output),
                label="C++ Bazel outputs",
            )

    @property
    def identity(self) -> ContentIdentity:
        value = {
            "schema": "literate-ai/standard-bazel-target@1",
            "component_revision": self.component_revision.uri,
            "build_system_resolver_identity": (self.build_system_resolver_identity.uri),
            "build_system_toolchain_identity": (
                self.build_system_toolchain_identity.uri
            ),
            "target_label": self.target_label,
            "output_path": self.output_path,
            "build_options": list(self.build_options),
        }
        if self.cpp_layout is not None:
            value["cpp_layout"] = self.cpp_layout.to_dict()
            value["cpp_test_output"] = self.cpp_test_output
        return canonical_identity(value)


def _copy_regular_file(source: Path, destination: Path) -> None:
    """Copy one admitted output through a bounded Windows sharing-delay window."""

    for delay in (*_WINDOWS_FRESH_OUTPUT_COPY_DELAYS, None):
        try:
            shutil.copy2(source, destination)
            return
        except PermissionError as exc:
            windows_code = getattr(exc, "winerror", None)
            if os.name != "nt" or windows_code not in {5, 32} or delay is None:
                raise
            time.sleep(delay)


def _copy_regular_tree(source: Path, destination: Path) -> None:
    """Copy a Bazel output without admitting links or special files."""

    if source.is_symlink():
        raise LocalStandardLifecycleError("Bazel target output cannot be a link")
    if source.is_file():
        if not stat.S_ISREG(source.stat().st_mode):
            raise LocalStandardLifecycleError(
                "Bazel target output must be a regular file or directory"
            )
        _copy_regular_file(source, destination)
        return
    if not source.is_dir():
        raise LocalStandardLifecycleError(
            "Bazel target output must be a regular file or directory"
        )
    destination.mkdir()
    found = False
    for child in sorted(source.rglob("*")):
        found = True
        relative = child.relative_to(source)
        target = destination / relative
        if child.is_symlink():
            raise LocalStandardLifecycleError(
                "Bazel target output cannot contain links"
            )
        if child.is_dir():
            target.mkdir(exist_ok=True)
        elif child.is_file() and stat.S_ISREG(child.stat().st_mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_regular_file(child, target)
        else:
            raise LocalStandardLifecycleError(
                "Bazel target output can contain only regular files"
            )
    if not found:
        raise LocalStandardLifecycleError("Bazel target directory output is empty")


def _copy_cpp_library_outputs(
    source: Path, export: Path, test_export: Path, target: StandardBazelTarget
) -> None:
    """Separate the declared consumer product from its generated test executable."""
    layout = target.cpp_layout
    if layout is None or target.cpp_test_output is None:
        raise LocalStandardLifecycleError(
            "C++ output projection lacks target authority"
        )
    if source.is_symlink() or not source.is_dir():
        raise LocalStandardLifecycleError("C++ Bazel output must be a regular tree")
    actual = set()
    for child in source.rglob("*"):
        if child.is_symlink() or not (child.is_dir() or child.is_file()):
            raise LocalStandardLifecycleError("C++ Bazel output contains unsafe nodes")
        if child.is_file():
            actual.add(child.relative_to(source).as_posix())
    expected = {*layout.files, target.cpp_test_output}
    if actual != expected:
        raise LocalStandardLifecycleError(
            "C++ Bazel output differs from declared file closure"
        )
    export.mkdir()
    for relative in layout.files:
        destination = export / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_regular_file(source / relative, destination)
    test_export.parent.mkdir(parents=True, exist_ok=True)
    _copy_regular_file(source / target.cpp_test_output, test_export)


class StandardBazelLifecyclePorts(LocalStandardLifecyclePorts):
    """Local Standard ports whose selected build nodes are produced by Bazel."""

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
        bazel_targets: tuple[StandardBazelTarget, ...],
        npm_targets: tuple[StandardNpmTarget, ...] = (),
        python_targets: tuple[StandardPythonTarget, ...] = (),
        python_wheelhouse: Path | None = None,
        provider_environment: Mapping[str, tuple[str, str]] | None = None,
        dependency_observation: HostDependencyObservation | None = None,
        independent_acceptance_oracle: LocalIndependentAcceptanceOracle | None = None,
        browser_driver: object | None = None,
        native_sdk_inputs: object | None = None,
        bazel_cache_arguments: tuple[str, ...] = (),
        shared_cache: BoundSharedCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
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
        self._bazel_resolution_builds: dict[str, dict[str, object]] = {}
        if not isinstance(bazel_cache_arguments, tuple) or any(
            not isinstance(item, str)
            or not item.startswith("--")
            or not item
            or "\x00" in item
            for item in bazel_cache_arguments
        ):
            raise ValueError("Bazel cache arguments must be exact --option tokens")
        if any(
            item == reserved or item.startswith(f"{reserved}=")
            for item in bazel_cache_arguments
            for reserved in ("--output_base", "--symlink_prefix")
        ):
            raise ValueError("Bazel cache arguments cannot change output custody")
        self.bazel_cache_arguments = bazel_cache_arguments
        if shared_cache is not None and not isinstance(shared_cache, BoundSharedCache):
            raise TypeError("shared cache must bind private configuration")
        self.shared_cache = shared_cache
        self.bazel_targets = {
            item.component_revision.uri: item for item in bazel_targets
        }
        if len(self.bazel_targets) != len(bazel_targets):
            raise ValueError("Standard Bazel targets must name unique Components")
        for revision, target in self.bazel_targets.items():
            contract = self.contracts.get(revision)
            if contract is None:
                raise ValueError("Bazel target names an unplanned Component")
            build_binding = contract.tool_binding(ComponentCommandPhase.BUILD)
            if (
                contract.locked_build_authority_identity != target.identity
                or contract.build_system_resolver_identity
                != target.build_system_resolver_identity
                or contract.build_system_toolchain_identity
                != target.build_system_toolchain_identity
                or build_binding.toolchain_identity
                != target.build_system_toolchain_identity
            ):
                raise ValueError(
                    "Standard Bazel target does not match locked build authority"
                )

    @staticmethod
    def _generated_files(root: Path) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise LocalStandardLifecycleError(
                    "generated Bazel source cannot contain links"
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise LocalStandardLifecycleError(
                    "generated Bazel source can contain only regular files"
                )
            files[path.relative_to(root).as_posix()] = path.read_bytes()
        return files

    @staticmethod
    def _evidence_file_records(root: Path) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for path in sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        ):
            if path.is_symlink():
                raise LocalStandardLifecycleError(
                    "Bazel dependency evidence cannot contain links"
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise LocalStandardLifecycleError(
                    "Bazel dependency evidence can contain only regular files"
                )
            relative = path.relative_to(root).as_posix()
            if relative == "evidence-manifest.json":
                continue
            content = path.read_bytes()
            records.append(
                {
                    "path": f".literate/bazel/{relative}",
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
            )
        return records

    def _write_bazel_dependency_evidence(
        self,
        plan: StandardComponentBuildPlan,
        artifact_root: Path,
        *,
        dependency: BazelDependencyEvidence,
        inputs: BazelBuildInputEvidence,
        build_toolchain_identity: ContentIdentity,
    ) -> dict[str, object]:
        evidence_root = artifact_root / ".literate" / "bazel"
        if evidence_root.exists() or evidence_root.is_symlink():
            raise LocalStandardLifecycleError(
                "build output occupied the reserved Bazel evidence path"
            )
        evidence_root.mkdir(parents=True)
        contents = {
            "MODULE.bazel.lock": dependency.lock_bytes,
            "module-graph.json": dependency.graph_bytes,
            "repositories.ndjson": dependency.repository_bytes,
            "buildfiles.txt": inputs.buildfiles_bytes,
            "source-inputs.txt": inputs.source_inputs_bytes,
            "build-input-consumption.json": canonical_json_bytes(
                inputs.consumption.to_dict()
            ),
        }
        for name, content in contents.items():
            (evidence_root / name).write_bytes(content)
        observation = dependency.observation
        manifest = {
            "schema": "urn:literate-ai:schema:v1:standard-bazel-dependency-evidence",
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-bazel-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "source_bundle_digest": plan.request.source_tree_identity.uri,
            "build_toolchain_identity": build_toolchain_identity.uri,
            "resolver_toolchain_identity": build_toolchain_identity.uri,
            "graph_identity": observation.graph_identity,
            "evidence_identity": observation.evidence_identity,
            "observation_identity": observation.observation_identity,
            "build_input_consumption": inputs.consumption.to_dict(),
            "build_input_consumption_identity": inputs.consumption.identity,
            "files": self._evidence_file_records(evidence_root),
        }
        manifest_bytes = canonical_json_bytes(manifest)
        (evidence_root / "evidence-manifest.json").write_bytes(manifest_bytes)
        return {
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-bazel-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "build_input_consumption": inputs.consumption.to_dict(),
            "build_input_consumption_identity": inputs.consumption.identity,
            "standard_bazel_evidence_manifest": _BAZEL_EVIDENCE_MANIFEST,
            "evidence_manifest_digest": "sha256:"
            + hashlib.sha256(manifest_bytes).hexdigest(),
        }

    def _write_resolved_sbom(
        self, plan: StandardComponentBuildPlan, artifact_root: Path
    ) -> CycloneDxBomBinding:
        evidence = self._bazel_resolution_builds.get(plan.identity.uri)
        if evidence is None:
            return super()._write_resolved_sbom(plan, artifact_root)
        observation = evidence["dependency_observation"]
        if not isinstance(observation, BuildDependencyObservation):
            raise LocalStandardLifecycleError(
                "Standard Bazel dependency evidence lost its typed observation"
            )
        artifact_digest = evidence["artifact_digest"]
        if not isinstance(artifact_digest, str):
            raise LocalStandardLifecycleError(
                "Standard Bazel dependency evidence lost its manifest identity"
            )
        additional = {
            key: value
            for key, value in evidence.items()
            if key not in {"artifact_digest", "dependency_observation"}
        }
        return self._resolve_and_write_sbom(
            plan,
            artifact_root,
            dependency_observation=observation,
            artifact_digest=artifact_digest,
            additional_build_evidence=additional,
        )

    def _build_locked(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        contract: ComponentCommandContract,
    ) -> StandardBuildOutput:
        target = self.bazel_targets.get(plan.component_revision.uri)
        if target is None:
            return super()._build_locked(plan, provider_artifacts, contract)
        return self._build_bazel_target(
            plan, provider_artifacts, contract=contract, target=target
        )

    def _build_bazel_target(
        self,
        plan: StandardComponentBuildPlan,
        provider_artifacts: tuple[ArtifactExport, ...],
        *,
        contract: ComponentCommandContract,
        target: StandardBazelTarget,
    ) -> StandardBuildOutput:
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
                "build plan does not realize the exact locked Bazel export shape"
            )
        if tuple(item.identity for item in provider_artifacts) != (
            plan.provider_artifact_identities
        ):
            raise LocalStandardLifecycleError(
                "Bazel builder received different provider artifacts"
            )

        source = self.source_trees.resolve(plan.request.source_tree_identity)
        source_identity = local_generated_source_tree_identity(source)
        provider_materials = self._provider_materials(
            provider_artifacts, consumer_revision=plan.component_revision
        )
        cache_identity = canonical_identity(
            {
                "schema": "literate-ai/local-standard-bazel-cache-key@1",
                "command_contract_identity": contract.identity.uri,
                "bazel_target_identity": target.identity.uri,
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
            )
            if reused is not None:
                self.build_cache_hits += 1
                self.build_cache_hit_seconds += time.monotonic() - started
                return reused

        staging = Path(tempfile.mkdtemp(prefix="standard-bazel-", dir=self.object_root))
        workspace = staging / "workspace"
        bazel_workspace = workspace / "source"
        object_workspace = staging / "objects"
        artifact_workspace = staging / "artifact"
        try:
            shutil.copytree(source, workspace)
            if (
                bazel_workspace.is_symlink()
                or not bazel_workspace.is_dir()
                or (bazel_workspace / "MODULE.bazel").is_symlink()
                or not (bazel_workspace / "MODULE.bazel").is_file()
            ):
                raise LocalStandardLifecycleError(
                    "generated Bazel workspace requires source/MODULE.bazel"
                )
            object_workspace.mkdir()
            artifact_workspace.mkdir()
            binding_identity = contract.tool_binding(
                ComponentCommandPhase.BUILD
            ).toolchain_identity
            binding = self.tool_bindings[binding_identity.uri]
            binding.require_unchanged()
            startup = [
                "--batch",
                "--nosystem_rc",
                "--nohome_rc",
                "--noworkspace_rc",
                f"--output_base={object_workspace.resolve()}",
            ]
            project_rc = bazel_workspace / ".bazelrc"
            if project_rc.exists():
                if project_rc.is_symlink() or not project_rc.is_file():
                    raise LocalStandardLifecycleError(
                        "generated Bazel workspace .bazelrc is unsafe"
                    )
                startup.insert(4, f"--bazelrc={project_rc}")

            compiler_binding = self.tool_bindings.get(
                contract.language_compiler_identity.uri
            )

            def run(phase: str, arguments: tuple[str, ...]):
                binding.require_unchanged()
                if compiler_binding is not None:
                    compiler_binding.require_unchanged()
                try:
                    credentials = (
                        self.shared_cache.bazel_credentials(object_workspace)
                        if self.shared_cache is not None
                        else nullcontext(())
                    )
                    with credentials as private_startup:
                        token = (
                            self.shared_cache.credential() if private_startup else None
                        )
                        result = self._run(
                            (*binding.command, *startup, *private_startup, *arguments),
                            cwd=bazel_workspace,
                            providers=provider_artifacts,
                            binding=compiler_binding or binding,
                            timeout_seconds=DEFAULT_BAZEL_COMMAND_TIMEOUT_SECONDS,
                            **(
                                {
                                    "extra_environment": {
                                        "LITAI_SHARED_CACHE_SECRET": token
                                    }
                                }
                                if token is not None
                                else {}
                            ),
                        )
                        if token is not None:
                            result.stdout = result.stdout.replace(token, "<redacted>")
                            result.stderr = result.stderr.replace(token, "<redacted>")
                        return result
                finally:
                    if self.shared_cache is not None:
                        self.shared_cache.require_unchanged()
                    binding.require_unchanged()
                    if compiler_binding is not None:
                        compiler_binding.require_unchanged()

            generated_files = self._generated_files(workspace)
            dependency_evidence = collect_bzlmod_dependency_evidence(
                run=run,
                workspace=bazel_workspace,
                source_bundle_digest=plan.request.source_tree_identity.uri,
                build_toolchain_identity=binding_identity.uri,
                resolver_toolchain_identity=binding_identity.uri,
            )
            build_input_evidence = collect_bazel_build_input_evidence(
                run=run,
                projection=workspace,
                workspace=bazel_workspace,
                generated_files=generated_files,
                source_bundle_digest=plan.request.source_tree_identity.uri,
                target_expression=target.target_label,
            )
            build_result = run(
                "build",
                (
                    "build",
                    "--lockfile_mode=error",
                    "--symlink_prefix=/",
                    *self.bazel_cache_arguments,
                    *target.build_options,
                    *(
                        self.shared_cache.bazel_arguments(workspace=staging)
                        if self.shared_cache is not None
                        else ()
                    ),
                    target.target_label,
                ),
            )
            info = run("info", ("info", "bazel-bin"))
            binding.require_unchanged()

            lines = tuple(
                line.strip() for line in info.stdout.splitlines() if line.strip()
            )
            if len(lines) != 1:
                raise LocalStandardLifecycleError(
                    "Bazel did not report one exact bazel-bin directory"
                )
            reported_bazel_bin = Path(lines[0])
            if not reported_bazel_bin.is_absolute() or reported_bazel_bin.is_symlink():
                raise LocalStandardLifecycleError(
                    "Bazel reported an unsafe bazel-bin directory"
                )
            bazel_bin = reported_bazel_bin.resolve(strict=True)
            object_root = object_workspace.resolve(strict=True)
            if not bazel_bin.is_dir() or object_root not in bazel_bin.parents:
                raise LocalStandardLifecycleError(
                    "Bazel reported an output directory outside Standard custody"
                )
            produced_path = bazel_bin / Path(*PurePosixPath(target.output_path).parts)
            cursor = bazel_bin
            for part in PurePosixPath(target.output_path).parts:
                cursor /= part
                if cursor.is_symlink():
                    raise LocalStandardLifecycleError(
                        "Bazel target output cannot be a link"
                    )
            # Opening the final Bazel output while canonicalizing it can yield
            # ACCESS_DENIED for a fresh executable or zipapp on Windows.  Custody is
            # already established lexically and by the component-wise reparse check.
            produced = produced_path
            if not produced.exists():
                raise LocalStandardLifecycleError(
                    "Bazel target did not produce its declared output"
                )
            if bazel_bin != produced and bazel_bin not in produced.parents:
                raise LocalStandardLifecycleError(
                    "Bazel target output escaped its reported output tree"
                )
            export_path = artifact_workspace / contract.artifact_export.export_id
            if target.cpp_layout is None:
                _copy_regular_tree(produced, export_path)
            else:
                if (
                    contract.library_import_surface is None
                    or contract.library_import_surface.language != "cpp"
                ):
                    raise LocalStandardLifecycleError(
                        "C++ Bazel output requires a C++ library contract"
                    )
                if contract.native_layout != target.cpp_layout:
                    raise LocalStandardLifecycleError(
                        "C++ Bazel target layout differs from command authority"
                    )
                suffix = ".exe" if target.cpp_test_output.endswith(".exe") else ""
                _copy_cpp_library_outputs(
                    produced,
                    export_path,
                    artifact_workspace / "generated-tests" / ("run" + suffix),
                    target,
                )
            if local_generated_source_tree_identity(source) != source_identity:
                raise LocalStandardLifecycleError(
                    "Bazel build mutated the admitted generated source tree"
                )
            process_observation = canonical_identity(
                {
                    "schema": "literate-ai/local-bazel-build-observation@1",
                    "build": self._process_observation(
                        build_result,
                        phase="bazel-build",
                        plan_identity=plan.identity,
                    ).uri,
                    "output_query": self._process_observation(
                        info,
                        phase="bazel-info",
                        plan_identity=plan.identity,
                    ).uri,
                    "target_identity": target.identity.uri,
                }
            )
            resolution_build = self._write_bazel_dependency_evidence(
                plan,
                artifact_workspace,
                dependency=dependency_evidence,
                inputs=build_input_evidence,
                build_toolchain_identity=binding_identity,
            )
            manifest_digest = resolution_build["evidence_manifest_digest"]
            if not isinstance(manifest_digest, str):
                raise LocalStandardLifecycleError(
                    "Standard Bazel dependency manifest has no identity"
                )
            self._bazel_resolution_builds[plan.identity.uri] = {
                **resolution_build,
                "artifact_digest": manifest_digest,
                "dependency_observation": dependency_evidence.observation,
            }
            try:
                self._write_artifact_manifest(
                    plan, artifact_workspace, provider_materials, process_observation
                )
            finally:
                self._bazel_resolution_builds.pop(plan.identity.uri, None)
            self._install_sealed_artifact(
                cache_identity,
                artifact,
                artifact_workspace,
                conflict="Bazel-addressed local artifact has conflicting bytes",
            )
            _remove_bazel_directory(staging)
        except Exception:
            if staging.exists():
                _remove_bazel_directory(staging)
            raise
        sealed = local_tree_identity(artifact)
        output = self._build_output(plan, artifact)
        if local_tree_identity(artifact) != sealed:
            raise LocalStandardLifecycleError(
                "Bazel-addressed local artifact changed before checkpoint publication"
            )
        self._publish_artifact_checkpoint(cache_identity, plan, artifact)
        self.build_cache_misses += 1
        self.build_seconds += time.monotonic() - started
        return output


__all__ = ["StandardBazelLifecyclePorts", "StandardBazelTarget"]
