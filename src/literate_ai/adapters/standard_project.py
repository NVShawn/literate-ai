"""Host adapters for planning a locked Standard Component project."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from literate_ai.adapters.builders import (
    DEFAULT_ZIG_MINIMUM_VERSION,
    BuildError,
    bazel_resolver_identity,
    discover_bazel_toolchain,
    discover_cargo_toolchain,
    discover_cmake_toolchain,
    discover_cpp_toolchain,
    discover_elixir_toolchain,
    discover_go_toolchain,
    discover_make_toolchain,
    discover_node_toolchain,
    discover_npm_toolchain,
    discover_python_toolchain,
    discover_rust_toolchain,
    discover_zig_toolchain,
)
from literate_ai.adapters.builders.cpp import bazel_sdk_build_options
from literate_ai.adapters.cache import (
    CachedCodingCliSourceGenerator,
    FilesystemStandardAcceptedSourcePublisher,
    FilesystemStandardLifecycleCheckpointStore,
    FilesystemStandardSourceRestorer,
    SourceCacheMaterializer,
    SourceCacheResolver,
)
from literate_ai.adapters.context_journal import (
    FilesystemForwardGenerationContextEvidenceRecorder,
    FilesystemForwardGenerationContextJournal,
)
from literate_ai.adapters.dependencies import (
    HostDependencyObservation,
    PortableHostDependencyObserver,
)
from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    GenerationExecutionPlanningAdapter,
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.lifecycle.cpp_acceptance import cpp_compile_driver_source
from literate_ai.adapters.lifecycle.standard_bazel import (
    StandardBazelLifecyclePorts,
    StandardBazelTarget,
)
from literate_ai.adapters.lifecycle.standard_cargo import (
    StandardCargoLifecyclePorts,
    StandardCargoTarget,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    RegisteredSourceGenerationRunner,
)
from literate_ai.adapters.lifecycle.standard_npm import StandardNpmTarget
from literate_ai.adapters.lifecycle.standard_python import StandardPythonTarget
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_ELIXIR_RUNTIME_DRIVER as _STANDARD_ELIXIR_RUNTIME_DRIVER,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_NATIVE_RUNTIME_DRIVER as _STANDARD_NATIVE_RUNTIME_DRIVER,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_NODE_RUNTIME_DRIVER as _STANDARD_NODE_RUNTIME_DRIVER,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_RUNTIME_DRIVER as _STANDARD_PYTHON_RUNTIME_DRIVER,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_WHEEL_RUNTIME_DRIVER,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.adapters.models import (
    CodingCliError,
    CodingCliSelection,
    CodingCliSourceGenerator,
    InheritedSessionSelection,
    InheritedSessionSourceGenerator,
    select_coding_cli,
    selected_coding_provider,
)
from literate_ai.adapters.models.coding_cli import (
    DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES,
)
from literate_ai.adapters.multi_entrypoint_build import standalone_driver_source
from literate_ai.adapters.source_generation import (
    CachedCodingCliSourceGenerationRunner,
    CodingCliSourceGenerationInvocation,
)
from literate_ai.application.component_execution_planning import (
    authored_assets_from_lock,
)
from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.application.library_artifacts import (
    project_cpp_library_layout,
    project_library_import_surface,
)
from literate_ai.application.release_artifacts import StandardReleaseDeclaration
from literate_ai.application.source_generation_scheduling import (
    ComponentSourceGenerationRunError,
    ComponentSourceGenerationRunner,
)
from literate_ai.application.standard_project_lifecycle import (
    AcceptedSourceCachePublisher,
    GenerationIndexer,
    StandardNodeAcceptedCandidate,
    StandardProjectLifecycleResult,
    StandardSourceCacheMembership,
)
from literate_ai.application.standard_project_services import (
    PreparedExecutableProject,
    StandardProjectApplicationService,
    assemble_standard_project_application_service,
)
from literate_ai.application.standard_source_admission import (
    StandardSourceAdmissionError,
    require_source_admission_for_worker,
)
from literate_ai.cache_directories import ensure_cache_directory
from literate_ai.contracts import (
    AuthoredBinaryAsset,
    BlobRef,
    ComponentLock,
    ContentIdentity,
    ContributionKind,
    FlavorAxis,
    InheritedSessionHandoffEvidence,
    SourceDerivationCacheKey,
    StandardAcceleratorCommandProfile,
    StandardArtifactLayout,
    StandardBuildSystemCommandProfile,
    StandardCargoCommandProfile,
    StandardCMakeCommandProfile,
    StandardLanguageBuildStrategy,
    StandardLanguageCommandProfile,
    StandardLanguageRuntimeStrategy,
    StandardLifecycleStageEvidence,
    StandardMakeCommandProfile,
    StandardNpmCommandProfile,
    StandardPlatformCommandProfile,
    StandardPythonWheelCommandProfile,
    StandardRepoManCommandProfile,
    StandardSourceAdmissionMembership,
    StandardSourceSelectorSet,
    ToolchainConstraint,
    merge_toolchain_constraints,
    parse_standard_command_profile,
    standard_entrypoint_source_path,
)
from literate_ai.contracts.components import (
    LITAI_SMOKE_MODE_FLAG,
    LITAI_TEST_MODE_FLAG,
)
from literate_ai.contracts.executable_components import (
    ComponentChangeSurface,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentContextBenchmarkRecord,
    ComponentExecutionPlan,
    ComponentInvalidationDecision,
    DependencyInputKind,
    ForwardGenerationContextCacheReport,
    GenerationComplexityBudget,
    ProjectSourceGenerationCustody,
    SourceBundleClosure,
    SourceGenerationResumeCandidate,
    StandardComponentCommandAuthority,
    StandardProviderArtifactBinding,
    StandardToolchainClosure,
)
from literate_ai.contracts.executable_components.commands import (
    ComponentArtifactExportShape,
    ComponentEntrypointCommandContract,
    ComponentLifecycleCommand,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.publication.standard_project import (
    ReleaseBlobReader,
    StandardProjectReleaseResult,
    StandardProjectReleaseService,
)
from literate_ai.security import SecurityProfile
from literate_ai.storage import FileSystemCAS


@dataclass(frozen=True, slots=True)
class PlannedStandardProject:
    """Exact Standard plan plus the host coding-CLI selection used to bind models."""

    coding_cli: CodingCliSelection
    execution_plan: ComponentExecutionPlan


class FilesystemStandardProjectPlanningAdapter:
    """Bind filesystem lock authority and PATH selection to the Standard planner."""

    def __init__(
        self,
        *,
        coding_cli_selector: Callable[[], CodingCliSelection] = select_coding_cli,
        model_selector: LockedComponentModelSelectionAdapter | None = None,
        native_sdk_inputs=None,
    ) -> None:
        self.native_sdk_inputs = native_sdk_inputs
        self.coding_cli_selector = coding_cli_selector
        self.model_selector = model_selector or LockedComponentModelSelectionAdapter()

    def plan(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        *,
        assets: tuple[AuthoredBinaryAsset, ...] | None = None,
    ) -> PlannedStandardProject:
        selection = self.coding_cli_selector()
        execution = self.plan_for_coding_cli(
            snapshot, coding_cli=selection.name, assets=assets
        )
        snapshot.require_unchanged()
        selection.require_unchanged()
        return PlannedStandardProject(selection, execution)

    def plan_for_coding_cli(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        *,
        coding_cli: str,
        assets: tuple[AuthoredBinaryAsset, ...] | None = None,
    ) -> ComponentExecutionPlan:
        """Plan against a logical provider without claiming an executable binding."""

        if not isinstance(snapshot, LockedGenerationAuthoritySnapshot):
            raise TypeError("snapshot must be a LockedGenerationAuthoritySnapshot")
        snapshot.require_unchanged()
        if assets is None:
            assets = authored_assets_from_lock(snapshot.authority.lock)
        from literate_ai.adapters.native_sdk_generation import (
            native_sdk_generation_identities,
        )

        sdk_identities = native_sdk_generation_identities(
            self.native_sdk_inputs, snapshot
        )
        model_identities = self.model_selector.identities(
            snapshot,
            coding_cli=coding_cli,
        )
        execution = StandardProjectApplicationService.plan(
            snapshot.authority.lock,
            model_identities=model_identities,
            assets=assets,
            native_sdk_input_identities=sdk_identities,
        )
        snapshot.require_unchanged()
        return execution


def admit_locked_authored_assets(
    snapshot: LockedGenerationAuthoritySnapshot,
    *,
    cas: FileSystemCAS,
) -> tuple[AuthoredBinaryAsset, ...]:
    """Admit exact locked asset bytes to one candidate CAS and return their bindings."""

    lock = snapshot.authority.lock
    for node in lock.nodes:
        for resolved in node.revision.assets:
            admitted = snapshot.admit_component_asset(
                node.revision.authoring_identity, resolved, cas
            )
            if admitted != resolved.blob:
                raise ValueError("locked asset bytes differ from their BlobRef")
    return authored_assets_from_lock(lock)


@dataclass(frozen=True, slots=True)
class StandardProjectRuntimeReadiness:
    """Honest production-readiness result for an explicitly assembled runtime."""

    ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectedStandardToolchainClosure:
    """Typed host realization of one exact locked Standard toolchain closure."""

    record: StandardToolchainClosure
    contracts: tuple[ComponentCommandContract, ...]
    tool_bindings: tuple[LocalComponentToolBinding, ...]
    toolchain_authorities: tuple[LocalObservedToolchainAuthority, ...]
    bazel_targets: tuple[StandardBazelTarget, ...]
    cargo_targets: tuple[StandardCargoTarget, ...]
    npm_targets: tuple[StandardNpmTarget, ...]
    provider_environment: Mapping[str, tuple[str, str]]
    dependency_observation: HostDependencyObservation
    python_targets: tuple[StandardPythonTarget, ...] = ()

    def require_unchanged(self) -> None:
        for binding in self.tool_bindings:
            binding.require_unchanged()
        for authority in self.toolchain_authorities:
            authority.require_unchanged()
        contracts = {
            item.component_revision.uri: item.identity for item in self.contracts
        }
        targets = {
            item.component_revision.uri: item.identity
            for item in (
                *self.bazel_targets,
                *self.cargo_targets,
                *self.npm_targets,
                *self.python_targets,
            )
        }
        authority_revisions = {
            item.component_revision.uri for item in self.record.component_authorities
        }
        authority_targets = {
            item.component_revision.uri
            for item in self.record.component_authorities
            if item.build_target_identity is not None
        }
        if (
            set(contracts) != authority_revisions
            or set(targets) != authority_targets
            or any(
                contracts.get(item.component_revision.uri)
                != item.command_contract_identity
                or targets.get(item.component_revision.uri)
                != item.build_target_identity
                for item in self.record.component_authorities
            )
        ):
            raise ValueError("Standard command or build-target authority changed")
        toolchains = tuple(
            sorted(
                (item.identity for item in self.toolchain_authorities),
                key=lambda item: item.uri,
            )
        )
        if toolchains != self.record.toolchain_identities:
            raise ValueError("Standard tool binding changed after projection")
        if (
            _provider_bindings(self.provider_environment)
            != self.record.provider_bindings
        ):
            raise ValueError("Standard provider binding changed after projection")
        if (
            _dependency_observation_identity(self.dependency_observation)
            != self.record.dependency_graph_identity
        ):
            raise ValueError("Standard dependency observation changed after projection")


@dataclass(frozen=True, slots=True)
class LocalObservedToolchainAuthority:
    """One exact discovered toolchain identity and its temporal drift guard."""

    identity: ContentIdentity
    _guard: Callable[[], None] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ContentIdentity):
            raise TypeError("observed toolchain identity must be a ContentIdentity")
        if not callable(self._guard):
            raise TypeError("observed toolchain guard must be callable")

    @classmethod
    def from_observed_toolchain(
        cls,
        toolchain: object,
        *,
        guard: Callable[[], None] | None = None,
    ) -> LocalObservedToolchainAuthority:
        raw_identity = getattr(toolchain, "identity", None)
        selected_guard = guard or getattr(toolchain, "require_unchanged", None)
        if not isinstance(raw_identity, str) or not callable(selected_guard):
            raise TypeError("observed toolchain must expose identity and drift guard")
        return cls(ContentIdentity.parse_uri(raw_identity), selected_guard)

    def require_unchanged(self) -> None:
        try:
            self._guard()
        except Exception as exc:
            raise ValueError("observed toolchain changed after projection") from exc


def _provider_bindings(
    values: Mapping[str, tuple[str, str]],
) -> tuple[StandardProviderArtifactBinding, ...]:
    for export_id, binding in values.items():
        if (
            not isinstance(export_id, str)
            or not isinstance(binding, tuple)
            or len(binding) != 2
            or any(not isinstance(item, str) for item in binding)
        ):
            raise ValueError("provider environment bindings must be typed pairs")
    return tuple(
        StandardProviderArtifactBinding(export_id, binding[0], binding[1])
        for export_id, binding in sorted(values.items())
    )


def _dependency_observation_identity(
    observation: HostDependencyObservation,
) -> ContentIdentity:
    if not isinstance(observation, HostDependencyObservation):
        raise TypeError("dependency observation must be a HostDependencyObservation")
    if not observation.components:
        raise ValueError("Standard toolchain dependency observation cannot be empty")
    components: list[dict[str, object]] = []
    seen_components: set[str] = set()
    for component in observation.components:
        if not isinstance(component, dict):
            raise ValueError("dependency observation components must be objects")
        component_identity = canonical_identity(component).uri
        if component_identity in seen_components:
            raise ValueError("dependency observation components must be unique")
        seen_components.add(component_identity)
        components.append(component)
    if any(
        not isinstance(edge, tuple)
        or len(edge) != 2
        or any(not isinstance(endpoint, str) or not endpoint for endpoint in edge)
        for edge in observation.edges
    ):
        raise ValueError("dependency observation edges must be unique string pairs")
    edges = tuple(sorted(observation.edges))
    if len(edges) != len(set(edges)):
        raise ValueError("dependency observation edges must be unique string pairs")
    return canonical_identity(
        {
            "schema": "literate-ai/standard-toolchain-dependency-graph@1",
            "components": sorted(
                components, key=lambda item: canonical_identity(item).uri
            ),
            "edges": [list(edge) for edge in edges],
        }
    )


class StandardCommandProjectionError(RuntimeError):
    """Locked Flavor command authority cannot be realized on this host."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class StandardAcceptedSourceContinuationError(RuntimeError):
    """Accepted-source-only execution has no exact verifier-admitted member."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


_STANDARD_BUILD_DRIVER = (
    "import base64,json,os,pathlib,shutil,subprocess,sys,zlib;"
    "strategy,compiler_json,compiler_environment_encoded,root,relative,obj,export="
    "sys.argv[1:8];"
    "source=pathlib.Path(root)/pathlib.PurePosixPath(relative);"
    "cpproot=pathlib.Path(root)/'source';"
    "cppsources=sorted(str(p) for p in cpproot.rglob('*.cpp') if p.is_file()) "
    "if strategy=='cpp-executable' else [];"
    "out=pathlib.Path(export);"
    "compiler=json.loads(compiler_json);"
    "compiler_environment=json.loads(zlib.decompress(base64.urlsafe_b64decode("
    "compiler_environment_encoded)).decode('utf-8'));"
    "environment=dict(os.environ);"
    "overridden=set(name.casefold() for name,value in compiler_environment);"
    "environment=dict((name,value) for name,value in environment.items() "
    "if os.name!='nt' or name.casefold() not in overridden);"
    "environment.update(compiler_environment);"
    "assert source.is_file() or strategy in "
    "('python-tree','javascript-tree','typescript-tree');"
    "pathlib.Path(obj).mkdir(parents=True,exist_ok=True);"
    "tree=strategy in "
    "('python-tree','javascript-tree','typescript-tree','elixir-tree');"
    "out.parent.mkdir(parents=True,exist_ok=True);"
    "shutil.rmtree(out) if tree and out.exists() else None;"
    "pattern=('*.py' if strategy=='python-tree' else "
    "'*.ts' if strategy=='typescript-tree' else '*.js');"
    "files=sorted(pathlib.Path(root).rglob(pattern));"
    "[compile(p.read_text(encoding='utf-8'),str(p),'exec') for p in files] "
    "if strategy=='python-tree' else None;"
    "elixir_files=sorted(p for p in pathlib.Path(root).rglob('*') "
    "if p.suffix in ('.ex','.exs') and p.is_file());"
    "elixir_check='Enum.each(System.argv(), fn path -> "
    "Code.string_to_quoted!(File.read!(path), file: path) end)';"
    "checks=[subprocess.run([*compiler,'-e',elixir_check,'--',"
    "*[str(p) for p in elixir_files]],capture_output=True,env=environment)] "
    "if strategy=='elixir-tree' else "
    "[subprocess.run([*compiler,'--check',str(p)],capture_output=True) "
    "for p in files] "
    "if strategy=='javascript-tree' else [];"
    "bad=next((p for p in checks if p.returncode),None);"
    "sys.stderr.buffer.write(bad.stderr) if bad else None;"
    "shutil.copytree(root,out) if tree and bad is None else None;"
    "cmd=([*compiler,'build-exe',str(source),'-femit-bin='+str(out)] "
    "if strategy=='zig-executable' else "
    "([*compiler,'--edition=2021',str(source),'-o',str(out)] "
    "if strategy=='rust-executable' else "
    "([*compiler,'build','-o',str(out),str(source)] "
    "if strategy=='go-executable' else "
    "([*compiler,str(source),'-o',str(out)] "
    "if strategy=='swift-executable' else "
    "([*compiler,'/nologo','/std:c++17','/EHsc','/I'+str(cpproot),"
    "*cppsources,'/Fe'+str(out)] "
    "if strategy=='cpp-executable' and pathlib.Path(compiler[0]).name.lower() in "
    "('cl','cl.exe','clang-cl','clang-cl.exe') else "
    "([*compiler,'-std=c++17','-O2','-I',str(cpproot),*cppsources,'-o',str(out)] "
    "if strategy=='cpp-executable' else None))))));"
    "built=subprocess.run(cmd,capture_output=True,env=environment) if cmd else None;"
    "sys.stdout.buffer.write(built.stdout) if built else None;"
    "sys.stderr.buffer.write(built.stderr) if built else None;"
    "raise SystemExit((bad.returncode if bad else 0) or "
    "(built.returncode if built else 0))"
)


def _inline_python_driver(source: str) -> str:
    """Encode verifier-owned Python without exposing brace tokens to validation."""

    encoded = base64.urlsafe_b64encode(
        zlib.compress(source.encode("utf-8"), level=9)
    ).decode("ascii")
    return (
        "exec(__import__('zlib').decompress("
        f"__import__('base64').urlsafe_b64decode('{encoded}')))"
    )


_STANDARD_LIBRARY_TEST_DRIVER = _inline_python_driver(
    """import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

language, tool_json, export_value, manifest, binary = sys.argv[1:6]
tool = json.loads(tool_json)
export = pathlib.Path(export_value).resolve(strict=True)
temporary = None
if language == "python":
    command = [*tool, str(export / "source" / "main.py"), "--litai-test"]
elif language == "javascript":
    command = [*tool, str(export / "source" / "main.js"), "--litai-test"]
elif language == "rust":
    temporary = pathlib.Path(tempfile.mkdtemp(prefix="litai-library-test-"))
    command = [*tool, "run", "--quiet", "--locked", "--manifest-path",
               str(export / manifest), "--bin", binary, "--target-dir",
               str(temporary / "target"), "--", "--litai-test"]
else:
    raise SystemExit("unsupported library language")
try:
    environment = dict(__import__("os").environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if language == "rust":
        native_command = [*tool, "test", "--locked", "--all-targets",
                          "--manifest-path", str(export / manifest),
                          "--target-dir", str(temporary / "target")]
        native = subprocess.run(
            native_command, cwd=export, capture_output=True, env=environment
        )
        # Native Cargo output is diagnostic evidence, not the JSON case protocol.
        sys.stderr.buffer.write(native.stdout)
        sys.stderr.buffer.write(native.stderr)
        if native.returncode:
            print("Cargo generated-test command failed: " + json.dumps(native_command),
                  file=sys.stderr)
            raise SystemExit(native.returncode)
    completed = subprocess.run(
        command, cwd=export, capture_output=True, env=environment
    )
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    raise SystemExit(completed.returncode)
finally:
    if temporary is not None:
        shutil.rmtree(temporary, ignore_errors=True)
"""
)


_STANDARD_LIBRARY_IMPORT_DRIVER = _inline_python_driver(
    """import base64
import importlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zlib

language, tool_json, surface_encoded, export_value, manifest = sys.argv[1:6]
sys.dont_write_bytecode = True
tool = json.loads(tool_json)
surface_json = zlib.decompress(
    base64.urlsafe_b64decode(surface_encoded)
).decode("utf-8")
surface = json.loads(surface_json)
export = pathlib.Path(export_value).resolve(strict=True)
source = (export / "source").resolve(strict=True)
observed = []
if language == "python":
    sys.path[:] = [str(source)]
    for capability in surface["capabilities"]:
        module = importlib.import_module(capability["module"])
        origin = pathlib.Path(module.__file__).resolve(strict=True)
        if source != origin and source not in origin.parents:
            raise RuntimeError("library import escaped the exact artifact")
        for symbol in capability["symbols"]:
            getattr(module, symbol)
        observed.append(capability["capability"])
elif language == "javascript":
    script = '''const fs=require('fs'),path=require('path');
const root=fs.realpathSync(process.argv[1]), surface=JSON.parse(process.argv[2]);
const packageRoot=fs.realpathSync(path.join(root,'source',surface.package));
const manifestPath=path.join(packageRoot,'package.json');
const manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8'));
if (manifest.name!==surface.package || !manifest.exports ||
    !['commonjs',undefined].includes(manifest.type))
  throw Error('library manifest differs from locked import authority');
const dependencyFields=['dependencies','devDependencies',
                        'optionalDependencies','peerDependencies'];
for (const field of dependencyFields)
  if (manifest[field] && Object.keys(manifest[field]).length)
    throw Error('dependency-free library manifest declares '+field);
const seen=[];
const expectedExports={};
for (const capability of surface.capabilities) {
  const prefix=surface.package+'/';
  if (!capability.module.startsWith(prefix))
    throw Error('library module escaped package authority');
  const subpath='./'+capability.module.slice(prefix.length);
  const targetSpec='./'+capability.module.slice(prefix.length)+'.js';
  expectedExports[subpath]=targetSpec;
  if (manifest.exports[subpath]!==targetSpec)
    throw Error('library exports map differs for '+subpath);
  const target=path.join(packageRoot,targetSpec);
  const resolved=fs.realpathSync(require.resolve(target));
  if (!(resolved===packageRoot || resolved.startsWith(packageRoot+path.sep)))
    throw Error('library import escaped exact artifact');
  const value=require(resolved);
  for (const symbol of capability.symbols)
    if (!(symbol in value)) throw Error('missing library symbol '+symbol);
  seen.push(capability.capability);
}
if (JSON.stringify(manifest.exports)!==JSON.stringify(expectedExports))
  throw Error('library exports map has undeclared entries');
process.stdout.write(JSON.stringify({schema:'literate-ai/library-import-observation@1',capabilities:seen}));
'''
    completed = subprocess.run([*tool, "-e", script, str(export), surface_json],
                               capture_output=True)
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    raise SystemExit(completed.returncode)
elif language == "rust":
    document = tomllib.loads((export / manifest).read_text(encoding="utf-8"))
    package_name = document["package"]["name"]
    if package_name.replace("-", "_") != surface["package"]:
        raise RuntimeError("Cargo package name differs from locked import authority")
    temporary = pathlib.Path(tempfile.mkdtemp(prefix="litai-library-import-"))
    try:
        uses = []
        for capability in surface["capabilities"]:
            for symbol in capability["symbols"]:
                uses.append("use " + capability["module"] + "::" + symbol + ";")
            observed.append(capability["capability"])
        (temporary / "src").mkdir()
        dependency_path = json.dumps(str((export / manifest).parent))
        (temporary / "Cargo.toml").write_text(
            '[package]\\nname="litai-import-verifier"\\nversion="0.0.0"\\nedition="2021"\\n'
            + "[dependencies]\\n" + surface["package"] + "={package="
            + json.dumps(package_name) + ",path=" + dependency_path + "}\\n",
            encoding="utf-8")
        (temporary / "src" / "main.rs").write_text(
            "\\n".join(uses) + "\\nfn main() {}\\n", encoding="utf-8")
        completed = subprocess.run([*tool, "check", "--quiet", "--offline",
                                    "--manifest-path", str(temporary / "Cargo.toml"),
                                    "--target-dir", str(temporary / "target")],
                                   capture_output=True)
        sys.stderr.buffer.write(completed.stderr)
        if completed.returncode:
            raise SystemExit(completed.returncode)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
else:
    raise SystemExit("unsupported library language")
if language != "javascript":
    print(json.dumps({"schema":"literate-ai/library-import-observation@1",
                      "capabilities":observed}, sort_keys=True, separators=(",",":")))
"""
)


_STANDARD_CPP_LIBRARY_TEST_DRIVER = _inline_python_driver(
    """import pathlib, subprocess, sys
command = [str(pathlib.Path(sys.argv[1]) / sys.argv[2]), "--litai-test"]
completed = subprocess.run(command, capture_output=True)
sys.stdout.buffer.write(completed.stdout)
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(completed.returncode)
"""
)

_STANDARD_CPP_LIBRARY_IMPORT_DRIVER = _inline_python_driver(
    cpp_compile_driver_source()
    + r"""
import base64, json, os, shutil, subprocess, sys, tempfile, zlib
compiler = json.loads(sys.argv[1])
surface = json.loads(zlib.decompress(base64.urlsafe_b64decode(sys.argv[2])))
layout = json.loads(zlib.decompress(base64.urlsafe_b64decode(sys.argv[3])))
export = Path(sys.argv[4]).resolve(strict=True)
environment = dict(os.environ)
overrides = dict(json.loads(zlib.decompress(base64.urlsafe_b64decode(sys.argv[5]))))
for key in tuple(environment):
    if key.casefold() in {name.casefold() for name in overrides}:
        del environment[key]
environment.update(overrides)
expected = set(layout["headers"] + layout["link_files"] + layout["runtime_files"])
actual = set()
for path in export.rglob("*"):
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        raise SystemExit("unsafe native library output")
    if path.is_file():
        actual.add(path.relative_to(export).as_posix())
if actual != expected:
    raise SystemExit("native library output closure differs")
root = Path(tempfile.mkdtemp(prefix="litai-cpp-import-"))
try:
    harness = root / "main.cpp"
    lines = []
    for capability in surface["capabilities"]:
        lines.append('#include "' + capability["module"] + '"')
        lines.extend("using ::" + symbol + ";" for symbol in capability["symbols"])
    harness.write_text("\n".join(lines) + "\nint main(){return 0;}\n", encoding="utf-8")
    executable = root / ("import.exe" if os.name == "nt" else "import")
    command, runtime_dirs = cpp_compile_command(
        compiler, export, layout, harness, executable
    )
    completed = subprocess.run(command, cwd=root, env=environment, capture_output=True)
    sys.stderr.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    if completed.returncode:
        raise SystemExit(completed.returncode)
    if os.name == "nt":
        environment["PATH"] = os.pathsep.join(
            (*runtime_dirs, environment.get("PATH", ""))
        )
    completed = subprocess.run(
        [str(executable)], cwd=root, env=environment, capture_output=True
    )
    sys.stderr.buffer.write(completed.stderr)
    if completed.returncode:
        raise SystemExit(completed.returncode)
    print(json.dumps({"schema":"literate-ai/library-import-observation@1",
        "capabilities":[item["capability"] for item in surface["capabilities"]]}))
finally:
    shutil.rmtree(root, ignore_errors=True)
"""
)


def _encoded_cpp_layout(layout) -> str:
    return base64.urlsafe_b64encode(
        zlib.compress(json.dumps(layout.to_dict(), separators=(",", ":")).encode())
    ).decode("ascii")


def _encoded_toolchain_environment(environment: object) -> str:
    """Encode a host environment into one bounded, shell-free command token.

    An MSVC developer environment commonly exceeds the portable 4096-character argv
    token limit even though it contains only four variables.  Compress the canonical
    pair list before it enters the command contract; the exact bytes remain part of the
    command identity and the lifecycle driver reconstructs them without a shell.
    """

    pairs = list(environment if isinstance(environment, tuple) else ())
    content = json.dumps(pairs, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(zlib.compress(content, level=9)).decode("ascii")
    if len(encoded) > 4096:
        raise StandardCommandProjectionError(
            "standard_command.toolchain_environment_oversized",
            "the discovered compiler environment cannot fit the bounded command "
            "contract",
        )
    return encoded


def _encoded_multi_entrypoint_outputs(outputs: object) -> str:
    content = json.dumps(outputs, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(zlib.compress(content, level=9)).decode("ascii")
    if len(encoded) > 4096:
        raise StandardCommandProjectionError(
            "standard_command.multi_entrypoint_outputs_oversized",
            "the declared entrypoint outputs cannot fit the bounded command contract",
        )
    return encoded


def _encoded_library_import_surface(surface: LibraryImportSurface) -> str:
    content = json.dumps(surface.to_dict(), separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(zlib.compress(content, level=9)).decode("ascii")
    if len(encoded) > 4096:
        raise StandardCommandProjectionError(
            "standard_command.library_import_surface_oversized",
            "the locked library import surface cannot fit the bounded verifier command",
        )
    return encoded


_STANDARD_MAKE_BUILD_DRIVER = (
    "import json,os,pathlib,subprocess,sys;"
    "make_json,language_tool,tool_env_json,source,obj,artifact,export,makefile,target="
    "sys.argv[1:10];"
    "make=json.loads(make_json);"
    "env=dict(os.environ);env.update(dict(json.loads(tool_env_json)));"
    "make_path=pathlib.Path(source)/pathlib.PurePosixPath(makefile);"
    "result=subprocess.run([*make,'-C',str(make_path.parent),'-f',make_path.name,"
    "'OBJECT_ROOT='+obj,"
    "'OUT='+artifact,'EXPORT_PATH='+export,"
    "'LITAI_LANGUAGE_TOOL='+language_tool,target],capture_output=True,env=env);"
    "sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr);"
    "raise SystemExit(result.returncode)"
)
_STANDARD_CMAKE_BUILD_DRIVER = (
    "import json,os,pathlib,subprocess,sys;"
    "cmake_json,language_tool,tool_env_json,source,obj,artifact,export,cmakelists,"
    "target=sys.argv[1:10];"
    "cmake=json.loads(cmake_json);"
    "env=dict(os.environ);env.update(dict(json.loads(tool_env_json)));"
    "list_path=pathlib.Path(source)/pathlib.PurePosixPath(cmakelists);"
    "configure=subprocess.run([*cmake,'-S',str(list_path.parent),'-B',obj,"
    "'-DOBJECT_ROOT='+obj,'-DOUT='+artifact,'-DEXPORT_PATH='+export,"
    "'-DLITAI_LANGUAGE_TOOL='+language_tool],capture_output=True,env=env);"
    "sys.stdout.buffer.write(configure.stdout);"
    "sys.stderr.buffer.write(configure.stderr);"
    "sys.exit(configure.returncode) if configure.returncode else None;"
    "result=subprocess.run([*cmake,'--build',obj,'--target',target],"
    "capture_output=True,env=env);"
    "sys.stdout.buffer.write(result.stdout);sys.stderr.buffer.write(result.stderr);"
    "raise SystemExit(result.returncode)"
)


def _host_platform_target() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in {"win32", "cygwin"}:
        return "windows"
    raise StandardCommandProjectionError(
        "standard_command.host_unsupported",
        f"the Standard command projector does not support {sys.platform!r}",
    )


def _profile_contributions(snapshot, node) -> tuple[object, ...]:
    selected = {item.uri for item in node.revision.selected_flavor_revisions}
    flavors = {item.identity.uri: item for item in snapshot.authority.selected_flavors}
    if not selected.issubset(flavors):
        raise StandardCommandProjectionError(
            "standard_command.flavor_missing",
            "a locked selected Flavor is absent from generation authority",
        )
    profiles: list[object] = []
    for identity in sorted(selected):
        flavor = flavors[identity]
        for contribution in flavor.definition.contributions:
            if (
                contribution.kind is not ContributionKind.BUILDER
                or contribution.content.kind != "standard-command-profile"
            ):
                continue
            try:
                content = snapshot.flavor_content(flavor.identity, contribution.content)
                value = json.loads(content.decode("utf-8"))
                profile = parse_standard_command_profile(
                    value,
                    path=(
                        f"Flavor {flavor.definition.coordinate.uri} contribution "
                        f"{contribution.contribution_id}"
                    ),
                )
            except Exception as exc:
                raise StandardCommandProjectionError(
                    "standard_command.profile_invalid",
                    "a selected Flavor has an invalid Standard command profile",
                ) from exc
            expected_target = flavor.definition.supported_targets[0]
            expected_type = {
                FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM: (
                    StandardLanguageCommandProfile
                ),
                FlavorAxis.BUILD_SYSTEM: (
                    StandardBuildSystemCommandProfile,
                    StandardRepoManCommandProfile,
                    StandardMakeCommandProfile,
                    StandardCMakeCommandProfile,
                    StandardCargoCommandProfile,
                ),
                FlavorAxis.PACKAGING: (
                    StandardNpmCommandProfile,
                    StandardPythonWheelCommandProfile,
                ),
                FlavorAxis.PLATFORM_OS: StandardPlatformCommandProfile,
                FlavorAxis.ACCELERATOR: StandardAcceleratorCommandProfile,
            }.get(flavor.definition.primary_axis)
            if expected_type is None or not isinstance(profile, expected_type):
                raise StandardCommandProjectionError(
                    "standard_command.profile_axis_mismatch",
                    "a Standard command profile does not match its Flavor axis",
                )
            if profile.target != expected_target:
                raise StandardCommandProjectionError(
                    "standard_command.profile_target_mismatch",
                    "a Standard command profile does not match its Flavor target",
                )
            profiles.append(profile)
    return tuple(profiles)


def _selected_flavor_revision_identity(
    snapshot,
    node,
    *,
    axis: FlavorAxis,
    target: str,
) -> ContentIdentity:
    selected = {item.uri for item in node.revision.selected_flavor_revisions}
    matches = tuple(
        flavor.identity
        for flavor in snapshot.authority.selected_flavors
        if flavor.identity.uri in selected
        and flavor.definition.primary_axis is axis
        and target in flavor.definition.supported_targets
    )
    if len(matches) != 1:
        raise StandardCommandProjectionError(
            "standard_command.flavor_cardinality",
            f"each Component requires exactly one selected {axis.value} Flavor "
            f"revision for target {target!r}",
        )
    return matches[0]


def _one_profile(values, expected_type, *, required: bool, label: str):
    selected = tuple(item for item in values if isinstance(item, expected_type))
    if (required and len(selected) != 1) or (not required and len(selected) > 1):
        requirement = "exactly one" if required else "at most one"
        raise StandardCommandProjectionError(
            "standard_command.profile_cardinality",
            f"each Component requires {requirement} {label} profile",
        )
    return selected[0] if selected else None


def _toolchain_constraints(snapshot, node) -> dict[str, ToolchainConstraint]:
    selected = {item.uri for item in node.revision.selected_flavor_revisions}
    by_name: dict[str, list[ToolchainConstraint]] = {}
    for flavor in snapshot.authority.selected_flavors:
        if flavor.identity.uri not in selected:
            continue
        for contribution in flavor.definition.contributions:
            if contribution.kind is not ContributionKind.TOOLCHAIN:
                continue
            try:
                raw = snapshot.flavor_content(flavor.identity, contribution.content)
                constraint = ToolchainConstraint.from_dict(
                    json.loads(raw.decode("utf-8")),
                    path=f"Flavor {flavor.definition.coordinate.uri} toolchain",
                )
            except Exception as exc:
                raise StandardCommandProjectionError(
                    "standard_command.toolchain_constraint_invalid",
                    "a selected Flavor has an invalid toolchain constraint",
                ) from exc
            by_name.setdefault(constraint.toolchain, []).append(constraint)
    return {
        name: merge_toolchain_constraints(tuple(values))
        for name, values in by_name.items()
    }


def _discover_toolchain(
    name: str,
    constraint: ToolchainConstraint | None,
    environment: Mapping[str, str],
):
    command = None if constraint is None else constraint.command
    minimum = None if constraint is None else constraint.minimum_version
    required = None if constraint is None else constraint.required_version
    if name == "elixir":
        try:
            return discover_elixir_toolchain(
                environment,
                pinned_command=command,
                minimum_version=minimum or (1, 18),
                required_version=required,
            )
        except BuildError as exc:
            remediation = (
                constraint.remediation
                if constraint is not None and constraint.remediation
                else "Install Elixir 1.18+ with Erlang/OTP 27+ "
                "and expose elixir on PATH."
            )
            code = "unavailable" if exc.code.endswith("_unavailable") else "invalid"
            raise StandardCommandProjectionError(
                f"standard_command.elixir_toolchain_{code}", f"{exc}; {remediation}"
            ) from exc
    if name == "python":
        return discover_python_toolchain(
            environment,
            pinned_command=command,
            minimum_version=minimum or (3, 11),
            required_version=required,
        )
    if name == "node":
        return discover_node_toolchain(
            environment,
            pinned_command=command,
            minimum_version=minimum or (20,),
            required_version=required,
        )
    if name == "rust":
        configured = dict(environment)
        if command is not None:
            configured["RUSTC"] = " ".join(command)
        return discover_rust_toolchain(configured)
    if name == "go":
        configured = dict(environment)
        if command is not None:
            configured["GOTOOLCHAIN_CMD"] = " ".join(command)
        return discover_go_toolchain(configured)
    if name == "cpp":
        configured = dict(environment)
        if command is not None:
            configured["CXX"] = " ".join(command)
        return discover_cpp_toolchain(configured)
    if name == "nvcc":
        configured = dict(environment)
        configured["CXX"] = " ".join(command or ("nvcc",))
        toolchain = discover_cpp_toolchain(configured)
        compiler = Path(toolchain.command[0])
        cuda_root = compiler.parent.parent
        library_root = cuda_root / ("lib" if (cuda_root / "lib").is_dir() else "lib64")
        if not library_root.is_dir():
            raise StandardCommandProjectionError(
                "standard_command.cuda_runtime_unavailable",
                "selected nvcc lacks its CUDA runtime library directory",
            )
        inherited_library = configured.get("LIBRARY_PATH", "")
        inherited_runtime = configured.get(
            "PATH" if os.name == "nt" else "LD_LIBRARY_PATH", ""
        )
        separator = os.pathsep
        runtime_name = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
        return replace(
            toolchain,
            environment=tuple(
                sorted(
                    {
                        "LIBRARY_PATH": str(library_root)
                        + (separator + inherited_library if inherited_library else ""),
                        runtime_name: str(library_root)
                        + (separator + inherited_runtime if inherited_runtime else ""),
                    }.items()
                )
            ),
        )
    if name == "swift":
        configured = dict(environment)
        configured["CXX"] = (
            " ".join(command)
            if command is not None
            else ("xcrun swiftc" if sys.platform == "darwin" else "swiftc")
        )
        try:
            return discover_cpp_toolchain(configured)
        except Exception as exc:
            if constraint is not None and constraint.remediation is not None:
                mitigation = constraint.remediation
            elif sys.platform == "darwin":
                mitigation = (
                    "install or repair Apple Command Line Tools with "
                    "'xcode-select --install', then verify 'xcrun swiftc --version'"
                )
            elif sys.platform.startswith("linux"):
                mitigation = (
                    "install a Swift toolchain supported by this Linux distribution "
                    "and expose swiftc on PATH; see "
                    "https://www.swift.org/install/linux/"
                )
            else:
                mitigation = (
                    "install the supported Swift toolchain for Windows and expose "
                    "swiftc.exe on PATH; see https://www.swift.org/install/windows/"
                )
            if constraint is not None and constraint.remediation_uri is not None:
                mitigation += f"; see {constraint.remediation_uri}"
            raise StandardCommandProjectionError(
                "standard_command.swift_toolchain_unavailable",
                f"selected Swift realization is unavailable: {mitigation}",
            ) from exc
    if name == "bazel":
        return discover_bazel_toolchain(environment, pinned_command=command)
    if name == "make":
        return discover_make_toolchain(
            environment,
            pinned_command=command,
            minimum_version=minimum or (3, 81),
            required_version=required,
        )
    if name == "cargo":
        return discover_cargo_toolchain(
            environment,
            pinned_command=command,
            minimum_version=minimum or (1, 70),
            required_version=required,
        )
    if name == "cmake":
        return discover_cmake_toolchain(
            environment,
            pinned_command=command,
            minimum_version=minimum or (3, 20),
            required_version=required,
        )
    if name in {"zig", "zig-cc"}:
        try:
            return discover_zig_toolchain(
                environment,
                pinned_command=command,
                minimum_version=minimum or DEFAULT_ZIG_MINIMUM_VERSION,
                required_version=required,
                default_command=("zig", "cc") if name == "zig-cc" else ("zig",),
            )
        except BuildError as exc:
            if constraint is not None and constraint.remediation is not None:
                mitigation = constraint.remediation
            else:
                mitigation = (
                    "install a supported Zig distribution, expose zig on PATH, "
                    "then verify 'zig version'; see https://ziglang.org/download/"
                )
            if constraint is not None and constraint.remediation_uri is not None:
                mitigation += f"; see {constraint.remediation_uri}"
            prefix = "zig_cc" if name == "zig-cc" else "zig"
            if exc.code.endswith("_unavailable"):
                code = f"standard_command.{prefix}_toolchain_unavailable"
                message = f"selected Zig realization is unavailable: {mitigation}"
            else:
                code = f"standard_command.{prefix}_toolchain_invalid"
                message = f"{exc}; {mitigation}"
            raise StandardCommandProjectionError(code, message) from exc
    raise StandardCommandProjectionError(
        "standard_command.toolchain_unsupported",
        f"no Standard host adapter exists for toolchain {name!r}",
    )


def _runtime_command(
    profile: StandardLanguageCommandProfile,
    *,
    single_file: bool,
    phase: ComponentCommandPhase,
    entrypoint_relative: str | None = None,
) -> tuple[str, ...]:
    mode = (
        LITAI_TEST_MODE_FLAG
        if phase is ComponentCommandPhase.TEST
        else LITAI_SMOKE_MODE_FLAG
    )
    layout = "file" if single_file else profile.artifact_layout.value
    common = ("{artifact_root}", "{export_path}")
    # ADR 0026: name the entrypoint's own module for a multi-entrypoint Component so
    # the runtime driver selects which surface to run; the single-entrypoint case
    # passes ``None`` and keeps the profile's fixed artifact entrypoint, so its
    # argv stays byte-identical to before multi-entrypoint existed.
    relative = (
        profile.artifact_entrypoint
        if entrypoint_relative is None
        else entrypoint_relative
    )
    if profile.runtime_strategy is StandardLanguageRuntimeStrategy.ELIXIR:
        return (
            "{tool}",
            "-e",
            _STANDARD_ELIXIR_RUNTIME_DRIVER,
            "--",
            *common,
            layout,
            relative,
            mode,
        )
    if profile.runtime_strategy is StandardLanguageRuntimeStrategy.PYTHON:
        return (
            "{tool}",
            "-c",
            _STANDARD_PYTHON_RUNTIME_DRIVER,
            *common,
            layout,
            relative,
            mode,
        )
    if profile.runtime_strategy is StandardLanguageRuntimeStrategy.JAVASCRIPT:
        return (
            "{tool}",
            "-e",
            _STANDARD_NODE_RUNTIME_DRIVER,
            *common,
            layout,
            relative,
            mode,
        )
    return (
        "{tool}",
        "-c",
        _STANDARD_NATIVE_RUNTIME_DRIVER,
        *common,
        mode,
    )


def _additional_entrypoint_export_id(
    primary_export_id: str, entrypoint_name: str, executable_suffix: str
) -> str:
    """Keep a platform executable suffix at the end of every export name."""

    stem = primary_export_id
    if executable_suffix and primary_export_id.endswith(executable_suffix):
        stem = primary_export_id[: -len(executable_suffix)]
    return f"{stem}-{entrypoint_name}{executable_suffix}"


def _library_import_surface(authoring, node, language: str) -> LibraryImportSurface:
    """Derive one exact package/import map from reviewed library authority."""

    if authoring.resolved_kind != "library":
        raise StandardCommandProjectionError(
            "standard_command.library_kind_required",
            "library import authority requires reviewed kind: library",
        )
    bindings = {item.capability: item for item in node.interface_bindings}
    missing = tuple(
        item.name
        for item in authoring.provides
        if item.interface is None or item.name not in bindings
    )
    if missing:
        raise StandardCommandProjectionError(
            "standard_command.library_interface_required",
            "every library capability requires one exact public interface: "
            + ", ".join(missing),
        )
    try:
        return project_library_import_surface(
            authoring.coordinate.name,
            language,
            tuple(
                (item.name, bindings[item.name].interface_identity)
                for item in authoring.provides
            ),
            declarations=authoring.library_imports,
        )
    except ValueError as exc:
        raise StandardCommandProjectionError(
            "standard_command.library_language_unsupported", str(exc)
        ) from exc


def _command_contract(
    plan,
    authoring,
    node,
    target_profile_identity: ContentIdentity,
    language: StandardLanguageCommandProfile,
    platform: StandardPlatformCommandProfile,
    build_system: (
        StandardBuildSystemCommandProfile
        | StandardMakeCommandProfile
        | StandardCMakeCommandProfile
        | StandardCargoCommandProfile
        | None
    ),
    tools: Mapping[str, object],
    accelerator: StandardAcceleratorCommandProfile | None = None,
    npm_profile: StandardNpmCommandProfile | None = None,
    npm_flavor_revision_identity: ContentIdentity | None = None,
    python_profile: StandardPythonWheelCommandProfile | None = None,
    python_flavor_revision_identity: ContentIdentity | None = None,
):
    cpp_library = language.target == "cpp" and authoring.resolved_kind == "library"
    if cpp_library and (
        getattr(language, "cpp_library_kind", None) is None
        or not isinstance(build_system, StandardBuildSystemCommandProfile)
    ):
        raise StandardCommandProjectionError(
            "standard_command.cpp_library_lifecycle_incomplete",
            "C++ libraries require an explicit native product kind and Bazel profile",
        )
    accelerator_toolchain = (
        accelerator is not None and language.target in accelerator.applies_to_languages
    )
    compiler = tools[
        accelerator.toolchain if accelerator_toolchain else language.toolchain
    ]
    entrypoints = tuple(authoring.entrypoints)
    library_surface = (
        _library_import_surface(authoring, node, language.target)
        if not entrypoints
        else None
    )
    native_layout = (
        project_cpp_library_layout(
            library_surface, kind=language.cpp_library_kind, platform=platform.target
        )
        if cpp_library
        else None
    )
    retained_repo_man = isinstance(build_system, StandardRepoManCommandProfile)
    single_file = build_system is not None and not retained_repo_man
    bazel = isinstance(build_system, StandardBuildSystemCommandProfile)
    target = None
    if (python_profile is None) != (python_flavor_revision_identity is None):
        raise StandardCommandProjectionError(
            "standard_command.python_authority_incomplete",
            "Python wheel commands require their profile and packaging Flavor revision",
        )
    if python_profile is not None and (
        language.target != "python"
        or language.build_strategy is not StandardLanguageBuildStrategy.PYTHON_TREE
        or language.runtime_strategy is not StandardLanguageRuntimeStrategy.PYTHON
        or language.artifact_layout is not StandardArtifactLayout.TREE
        or build_system is not None
        or npm_profile is not None
        or library_surface is not None
        or len(entrypoints) != 1
        or accelerator_toolchain
    ):
        raise StandardCommandProjectionError(
            "standard_command.python_wheel_profile_unsupported",
            "Python wheels require one Python application entrypoint without a "
            "competing build-system, compiler or packaging profile",
        )
    export_id = f"artifact-{plan.component_revision.digest[:20]}"
    if (
        library_surface is None
        and language.runtime_strategy
        is StandardLanguageRuntimeStrategy.NATIVE_EXECUTABLE
        and platform.executable_suffix
        and not export_id.endswith(platform.executable_suffix)
    ):
        export_id += platform.executable_suffix
    entrypoint_export_ids = tuple(
        export_id
        if index == 0
        else _additional_entrypoint_export_id(
            export_id, entrypoint.name, platform.executable_suffix
        )
        for index, entrypoint in enumerate(entrypoints)
    )
    if len(entrypoints) > 1 and build_system is not None:
        raise StandardCommandProjectionError(
            "standard_command.multi_entrypoint_build_profile_unsupported",
            "the selected build-system profile declares one output target and "
            "cannot truthfully realize multiple Component entrypoints; select the "
            "language-native build profile or add an exact multi-output mapping",
        )
    if (npm_profile is None) != (npm_flavor_revision_identity is None):
        raise StandardCommandProjectionError(
            "standard_command.npm_authority_incomplete",
            "npm command authority requires both its profile and Flavor revision",
        )
    if npm_profile is not None:
        if language.target != "javascript":
            raise StandardCommandProjectionError(
                "standard_command.npm_language_unsupported",
                "the npm lifecycle requires the JavaScript language Flavor",
            )
        if build_system is not None:
            raise StandardCommandProjectionError(
                "standard_command.npm_build_system_unsupported",
                "package-npm cannot be combined with an explicit Standard "
                "build-system profile until a combined authority is defined",
            )
        npm_tool = tools[npm_profile.toolchain]
        if (
            getattr(getattr(npm_tool, "node", None), "identity", None)
            != compiler.identity
        ):
            raise StandardCommandProjectionError(
                "standard_command.npm_node_mismatch",
                "selected npm does not belong to the selected Node.js toolchain",
            )
        build_system_tool = npm_tool
        build_command_tool = npm_tool
        resolver = canonical_identity(
            {
                "schema": "literate-ai/standard-npm-build-resolver@1",
                "packaging_flavor_revision_identity": (
                    npm_flavor_revision_identity.uri
                ),
                "packaging_profile_identity": npm_profile.identity.uri,
                "npm_toolchain_identity": npm_tool.identity,
                "node_toolchain_identity": compiler.identity,
            }
        )
        target = StandardNpmTarget(
            plan.component_revision,
            npm_flavor_revision_identity,
            npm_profile.identity,
            resolver,
            ContentIdentity.parse_uri(npm_tool.identity),
            ContentIdentity.parse_uri(compiler.identity),
            npm_profile.manifest,
            npm_profile.lockfile,
            npm_tool.command,
        )
        build_argv = (
            "{tool}",
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--no-bin-links",
            "{source_root}",
            "{object_root}",
            "{export_path}",
        )
    elif build_system is None or retained_repo_man:
        python = tools.get("python")
        if python is None:
            raise StandardCommandProjectionError(
                "standard_command.python_driver_missing",
                "native Standard builds require the Python lifecycle driver",
            )
        build_system_tool = python
        build_command_tool = python
        resolver = canonical_identity(
            {
                "schema": "literate-ai/standard-native-build-resolver@1",
                "language_profile_identity": language.identity.uri,
                "platform_profile_identity": platform.identity.uri,
            }
        )
        if library_surface is not None:
            if language.target not in {"python", "javascript"}:
                raise StandardCommandProjectionError(
                    "standard_command.library_build_profile_required",
                    "Rust importable libraries require the Cargo build profile",
                )
            build_argv = (
                "{tool}",
                "-c",
                _STANDARD_BUILD_DRIVER,
                language.build_strategy.value,
                json.dumps(compiler.command, separators=(",", ":")),
                _encoded_toolchain_environment(getattr(compiler, "environment", ())),
                "{source_root}",
                ".",
                "{object_root}",
                "{export_path}",
            )
        elif len(entrypoints) > 1:
            output_descriptors = [
                {
                    "source": standard_entrypoint_source_path(
                        language.source_entrypoint,
                        entrypoint.path,
                        primary=index == 0,
                    ),
                    "export_id": entrypoint_export_ids[index],
                }
                for index, entrypoint in enumerate(entrypoints)
            ]
            build_argv = (
                "{tool}",
                "-c",
                standalone_driver_source(),
                language.build_strategy.value,
                json.dumps(compiler.command, separators=(",", ":")),
                _encoded_toolchain_environment(getattr(compiler, "environment", ())),
                "{source_root}",
                "{object_root}",
                "{artifact_root}",
                "{export_path}",
                _encoded_multi_entrypoint_outputs(output_descriptors),
            )
        else:
            build_argv = (
                "{tool}",
                "-c",
                _STANDARD_BUILD_DRIVER,
                language.build_strategy.value,
                json.dumps(compiler.command, separators=(",", ":")),
                _encoded_toolchain_environment(getattr(compiler, "environment", ())),
                "{source_root}",
                language.source_entrypoint,
                "{object_root}",
                "{export_path}",
            )
    elif bazel:
        build_system_tool = tools[build_system.toolchain]
        build_command_tool = build_system_tool
        try:
            resolver = bazel_resolver_identity(build_system_tool)
        except (AttributeError, TypeError):
            resolver = canonical_identity(
                {
                    "schema": "literate-ai/standard-build-system-resolver@1",
                    "profile_identity": build_system.identity.uri,
                    "toolchain_identity": build_system_tool.identity,
                }
            )
        output = "library" if cpp_library else language.bazel_output_path
        if (
            not cpp_library
            and language.runtime_strategy
            is StandardLanguageRuntimeStrategy.NATIVE_EXECUTABLE
            and platform.executable_suffix
            and not output.endswith(platform.executable_suffix)
        ):
            output += platform.executable_suffix
        target = StandardBazelTarget(
            plan.component_revision,
            resolver,
            ContentIdentity.parse_uri(build_system_tool.identity),
            build_system.target_label,
            output,
            build_options=bazel_sdk_build_options(getattr(compiler, "environment", ())),
            cpp_layout=native_layout,
            cpp_test_output=(
                "tests/run" + platform.executable_suffix if cpp_library else None
            ),
        )
        build_argv = (
            "{tool}",
            "build",
            build_system.target_label,
            "{source_root}",
            "{object_root}",
            "{export_path}",
        )
    elif isinstance(build_system, StandardMakeCommandProfile):
        build_system_tool = tools[build_system.toolchain]
        build_command_tool = tools["python"]
        if len(compiler.command) != 1:
            raise StandardCommandProjectionError(
                "standard_command.make_language_tool_unsupported",
                "the Make lifecycle requires one exact language-tool executable",
            )
        resolver = canonical_identity(
            {
                "schema": "literate-ai/standard-make-build-resolver@1",
                "profile_identity": build_system.identity.uri,
                "toolchain_identity": build_system_tool.identity,
            }
        )
        build_argv = (
            "{tool}",
            "-c",
            _STANDARD_MAKE_BUILD_DRIVER,
            json.dumps(build_system_tool.command, separators=(",", ":")),
            compiler.command[0],
            json.dumps(
                list(getattr(compiler, "environment", ())), separators=(",", ":")
            ),
            "{source_root}",
            "{object_root}",
            "{artifact_root}",
            "{export_path}",
            build_system.makefile,
            build_system.build_target,
        )
    elif isinstance(build_system, StandardCMakeCommandProfile):
        build_system_tool = tools[build_system.toolchain]
        build_command_tool = tools["python"]
        if len(compiler.command) != 1:
            raise StandardCommandProjectionError(
                "standard_command.cmake_language_tool_unsupported",
                "the CMake lifecycle requires one exact language-tool executable",
            )
        resolver = canonical_identity(
            {
                "schema": "literate-ai/standard-cmake-build-resolver@1",
                "profile_identity": build_system.identity.uri,
                "toolchain_identity": build_system_tool.identity,
            }
        )
        build_argv = (
            "{tool}",
            "-c",
            _STANDARD_CMAKE_BUILD_DRIVER,
            json.dumps(build_system_tool.command, separators=(",", ":")),
            compiler.command[0],
            json.dumps(
                list(getattr(compiler, "environment", ())), separators=(",", ":")
            ),
            "{source_root}",
            "{object_root}",
            "{artifact_root}",
            "{export_path}",
            build_system.cmakelists,
            build_system.build_target,
        )
    elif isinstance(build_system, StandardCargoCommandProfile):
        if language.target != "rust":
            raise StandardCommandProjectionError(
                "standard_command.cargo_language_unsupported",
                "the Cargo lifecycle requires the Rust language Flavor",
            )
        build_system_tool = tools[build_system.toolchain]
        build_command_tool = build_system_tool
        if len(compiler.command) != 1:
            raise StandardCommandProjectionError(
                "standard_command.cargo_language_tool_unsupported",
                "the Cargo lifecycle requires one exact Rust compiler executable",
            )
        resolver = canonical_identity(
            {
                "schema": "literate-ai/standard-cargo-build-resolver@1",
                "profile_identity": build_system.identity.uri,
                "toolchain_identity": build_system_tool.identity,
            }
        )
        target = StandardCargoTarget(
            plan.component_revision,
            resolver,
            ContentIdentity.parse_uri(build_system_tool.identity),
            ContentIdentity.parse_uri(compiler.identity),
            build_system.manifest,
            build_system.binary,
            compiler.command,
            library=library_surface is not None,
        )
        build_argv = (
            "{tool}",
            "build",
            "--locked",
            "--manifest-path",
            build_system.manifest,
            *(
                ("--lib",)
                if library_surface is not None
                else ("--bin", build_system.binary)
            ),
            "{source_root}",
            "{object_root}",
            "{export_path}",
        )
    else:
        raise StandardCommandProjectionError(
            "standard_command.build_system_unsupported",
            f"Standard has no command adapter for build system {build_system.target!r}",
        )
    if python_profile is not None:
        build_argv = (build_argv[0], "-I", "-S", "-B", *build_argv[1:])
        resolver = canonical_identity(
            {
                "schema": "literate-ai/standard-python-wheel-build-resolver@1",
                "packaging_profile_identity": python_profile.identity.uri,
                "packaging_flavor_revision_identity": (
                    python_flavor_revision_identity.uri
                ),
                "python_toolchain_identity": compiler.identity,
            }
        )
        target = StandardPythonTarget(
            plan.component_revision,
            python_flavor_revision_identity,
            python_profile.identity,
            resolver,
            ContentIdentity.parse_uri(compiler.identity),
            ContentIdentity.parse_uri(compiler.identity),
            python_profile.manifest,
            python_profile.lockfile,
            compiler.command,
        )
    # ADR 0026: multiple entrypoints are now first-class. The top-level command
    # fields still project the primary (root) entrypoint verbatim so single-
    # entrypoint Components stay byte-identical; additional entrypoints, when
    # present, are carried in the additive ``entrypoint_contracts`` map below.
    build_command = ComponentLifecycleCommand(ComponentCommandPhase.BUILD, build_argv)
    if library_surface is not None:
        runtime_tool = tools.get("python")
        if runtime_tool is None:
            raise StandardCommandProjectionError(
                "standard_command.python_driver_missing",
                "library verification requires the Python lifecycle driver",
            )
        cargo_library = isinstance(build_system, StandardCargoCommandProfile)
        verification_tool = build_system_tool if cargo_library else compiler
        manifest = build_system.manifest if cargo_library else "-"
        binary = build_system.binary if cargo_library else "-"
        test_argv = (
            "{tool}",
            "-c",
            _STANDARD_LIBRARY_TEST_DRIVER,
            language.target,
            json.dumps(verification_tool.command, separators=(",", ":")),
            "{export_path}",
            manifest,
            binary,
            "{artifact_root}",
        )
        execute_argv = (
            "{tool}",
            "-c",
            _STANDARD_LIBRARY_IMPORT_DRIVER,
            language.target,
            json.dumps(verification_tool.command, separators=(",", ":")),
            _encoded_library_import_surface(library_surface),
            "{export_path}",
            manifest,
            "{artifact_root}",
        )
    else:
        test_argv = _runtime_command(
            language, single_file=single_file, phase=ComponentCommandPhase.TEST
        )
        execute_argv = _runtime_command(
            language, single_file=single_file, phase=ComponentCommandPhase.EXECUTE
        )
    if python_profile is not None:

        def wheel_argv(argv):
            return (
                argv[0],
                "-I",
                "-S",
                "-B",
                "-c",
                STANDARD_PYTHON_WHEEL_RUNTIME_DRIVER,
                *argv[3:],
            )

        test_argv = wheel_argv(test_argv)
        execute_argv = wheel_argv(execute_argv)
    if cpp_library:
        test_argv = (
            "{tool}",
            "-c",
            _STANDARD_CPP_LIBRARY_TEST_DRIVER,
            "{artifact_root}",
            "generated-tests/run" + platform.executable_suffix,
        )
        execute_argv = (
            "{tool}",
            "-c",
            _STANDARD_CPP_LIBRARY_IMPORT_DRIVER,
            json.dumps(compiler.command, separators=(",", ":")),
            _encoded_library_import_surface(library_surface),
            _encoded_cpp_layout(native_layout),
            "{export_path}",
            _encoded_toolchain_environment(getattr(compiler, "environment", ())),
            "{artifact_root}",
        )
    test_command = ComponentLifecycleCommand(ComponentCommandPhase.TEST, test_argv)
    execute_command = ComponentLifecycleCommand(
        ComponentCommandPhase.EXECUTE, execute_argv
    )
    if library_surface is not None:
        pass
    elif language.runtime_strategy in {
        StandardLanguageRuntimeStrategy.PYTHON,
        StandardLanguageRuntimeStrategy.JAVASCRIPT,
        StandardLanguageRuntimeStrategy.ELIXIR,
    }:
        runtime_tool = compiler
    else:
        runtime_tool = tools.get("python")
        if runtime_tool is None:
            raise StandardCommandProjectionError(
                "standard_command.python_driver_missing",
                "native executable dispatch requires the Python lifecycle driver",
            )
    commands = (build_command, test_command, execute_command)
    phase_tools = (build_command_tool, runtime_tool, runtime_tool)
    producer = canonical_identity(
        {
            "schema": "literate-ai/standard-artifact-producer@1",
            "language_profile_identity": language.identity.uri,
            "platform_profile_identity": platform.identity.uri,
            "build_system_profile_identity": (
                None if build_system is None else build_system.identity.uri
            ),
            "packaging_profile_identity": (
                python_profile.identity.uri
                if python_profile is not None
                else None
                if npm_profile is None
                else npm_profile.identity.uri
            ),
            "packaging_flavor_revision_identity": (
                python_flavor_revision_identity.uri
                if python_flavor_revision_identity is not None
                else None
                if npm_flavor_revision_identity is None
                else npm_flavor_revision_identity.uri
            ),
            "build_toolchain_identity": build_system_tool.identity,
            "compiler_identity": compiler.identity,
        }
    )

    def _entrypoint_export(entrypoint, entrypoint_export_id: str):
        return ComponentArtifactExportShape(
            entrypoint_export_id,
            entrypoint.kind,
            canonical_identity(
                {
                    "schema": "literate-ai/portable-json-application-abi@1",
                    "entrypoint": entrypoint.to_dict(),
                    "public_interfaces": [
                        item.uri
                        for item in (
                            plan.generation_key.direct_public_interface_identities
                        )
                    ],
                }
            ),
            target_profile_identity,
            language.media_type,
            producer,
        )

    # Executables retain their legacy primary export byte-for-byte. ADR 0038
    # libraries bind a package tree to a typed import surface instead.
    shape = (
        ComponentArtifactExportShape(
            export_id,
            "library",
            canonical_identity(
                {
                    "schema": "literate-ai/importable-library-abi@1",
                    "component_revision": plan.component_revision.uri,
                    "public_interfaces": [
                        item.uri
                        for item in (
                            plan.generation_key.exported_public_interface_identities
                        )
                    ],
                    "import_surface_identity": library_surface.identity.uri,
                    **(
                        {
                            "native_layout": native_layout.to_dict(),
                            "language_profile_identity": language.identity.uri,
                            "platform_profile_identity": platform.identity.uri,
                            "target_profile_identity": target_profile_identity.uri,
                            "compiler_identity": compiler.identity,
                        }
                        if native_layout is not None
                        else {}
                    ),
                }
            ),
            target_profile_identity,
            f"application/vnd.literate-ai.{language.target}-package-tree",
            producer,
        )
        if library_surface is not None
        else _entrypoint_export(entrypoints[0], export_id)
    )
    # Fan TEST/EXECUTE out per entrypoint only for genuine multi-entrypoint
    # Components. Single- and zero-entrypoint Components keep ``None`` so their
    # wire bytes and identity never change. Each per-entrypoint contract binds the
    # entrypoint's own runtime argv, export shape, and deployment unit; BUILD stays
    # the single per-Component-revision command above.
    entrypoint_contracts: tuple[ComponentEntrypointCommandContract, ...] | None = None
    if len(entrypoints) > 1:
        runtime_binding_identity = ContentIdentity.parse_uri(runtime_tool.identity)
        per_entrypoint: list[ComponentEntrypointCommandContract] = []
        for index, entrypoint in enumerate(entrypoints):
            if index == 0:
                entrypoint_export_id = export_id
                entrypoint_shape = shape
            else:
                entrypoint_export_id = entrypoint_export_ids[index]
                entrypoint_shape = _entrypoint_export(entrypoint, entrypoint_export_id)
            entrypoint_relative = standard_entrypoint_source_path(
                language.source_entrypoint,
                entrypoint.path,
                primary=index == 0,
            )
            entrypoint_test_argv = _runtime_command(
                language,
                single_file=single_file,
                phase=ComponentCommandPhase.TEST,
                entrypoint_relative=entrypoint_relative,
            )
            entrypoint_execute_argv = _runtime_command(
                language,
                single_file=single_file,
                phase=ComponentCommandPhase.EXECUTE,
                entrypoint_relative=entrypoint_relative,
            )
            per_entrypoint.append(
                ComponentEntrypointCommandContract(
                    entrypoint_identity=entrypoint_shape.identity,
                    deployment_unit=entrypoint.resolved_deployment_unit,
                    commands=(
                        ComponentLifecycleCommand(
                            ComponentCommandPhase.TEST, entrypoint_test_argv
                        ),
                        ComponentLifecycleCommand(
                            ComponentCommandPhase.EXECUTE, entrypoint_execute_argv
                        ),
                    ),
                    tool_bindings=(
                        ComponentCommandToolBinding(
                            ComponentCommandPhase.TEST, runtime_binding_identity
                        ),
                        ComponentCommandToolBinding(
                            ComponentCommandPhase.EXECUTE, runtime_binding_identity
                        ),
                    ),
                    artifact_export=entrypoint_shape,
                )
            )
        entrypoint_contracts = tuple(per_entrypoint)
    if target is None:
        build_authority = canonical_identity(
            {
                "schema": "literate-ai/standard-native-command-authority@1",
                "language_profile_identity": language.identity.uri,
                "platform_profile_identity": platform.identity.uri,
                "build_command_identity": build_command.identity.uri,
                "build_toolchain_identity": build_system_tool.identity,
                "compiler_identity": compiler.identity,
            }
        )
    else:
        build_authority = target.identity
    contract = ComponentCommandContract(
        component_revision=plan.component_revision,
        locked_build_authority_identity=build_authority,
        build_system_resolver_identity=resolver,
        build_system_toolchain_identity=ContentIdentity.parse_uri(
            build_system_tool.identity
        ),
        language_compiler_identity=ContentIdentity.parse_uri(compiler.identity),
        language_runtime_identity=ContentIdentity.parse_uri(runtime_tool.identity),
        commands=commands,
        tool_bindings=tuple(
            ComponentCommandToolBinding(phase, ContentIdentity.parse_uri(tool.identity))
            for phase, tool in zip(ComponentCommandPhase, phase_tools, strict=True)
        ),
        artifact_export=shape,
        entrypoint_contracts=entrypoint_contracts,
        library_import_surface=library_surface,
        native_layout=native_layout,
    )
    return contract, target, language


def project_locked_standard_toolchain_closure(
    snapshot: LockedGenerationAuthoritySnapshot,
    execution_plan: ComponentExecutionPlan,
    *,
    environment: Mapping[str, str] | None = None,
    host_platform: str | None = None,
    toolchain_discoverer: Callable[
        [str, ToolchainConstraint | None, Mapping[str, str]], object
    ]
    | None = None,
    npm_toolchain_discoverer: Callable[[object, Mapping[str, str]], object]
    | None = None,
    dependency_observer: Callable[
        [tuple[tuple[str, ...], ...]], HostDependencyObservation
    ]
    | None = None,
    native_sdk_inputs=None,
) -> ProjectedStandardToolchainClosure:
    """Derive all Standard command authority from one exact lock and its Flavors."""

    if not isinstance(snapshot, LockedGenerationAuthoritySnapshot) and not (
        hasattr(snapshot, "authority")
        and callable(getattr(snapshot, "flavor_content", None))
        and callable(getattr(snapshot, "require_unchanged", None))
    ):
        raise TypeError("snapshot must provide locked generation authority")
    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    snapshot.require_unchanged()
    if execution_plan.component_lock_identity != snapshot.authority.lock.identity:
        raise StandardCommandProjectionError(
            "standard_command.plan_mismatch",
            "execution plan does not belong to the locked generation authority",
        )
    from literate_ai.adapters.native_sdk_commands import project_native_sdk_commands
    from literate_ai.adapters.native_sdk_generation import (
        native_sdk_generation_identities,
    )

    sdk_identities = native_sdk_generation_identities(native_sdk_inputs, snapshot) or {}
    if native_sdk_inputs is None and any(
        node.revision.repository_sources for node in snapshot.authority.lock.nodes
    ):
        raise StandardCommandProjectionError(
            "standard_command.native_sdk_admission_required",
            "repository source commands require live admitted SDK inputs",
        )
    for plan in execution_plan.generation_plans:
        if plan.generation_key.native_sdk_input_identities != sdk_identities.get(
            plan.component_revision.uri, ()
        ):
            raise StandardCommandProjectionError(
                "standard_command.native_sdk_input_mismatch",
                "command projection requires the generation plan's live SDK inputs",
            )
    from literate_ai.adapters.native_sdk_linked_runtime import (
        native_sdk_runtime_revisions,
    )

    sdk_runtime_revisions = {
        plan.component_revision.uri
        for plan in execution_plan.generation_plans
        if native_sdk_inputs is not None
        and any(
            sdk_identities.get(owner.uri)
            for owner in native_sdk_runtime_revisions(snapshot, plan.component_revision)
        )
    }
    configured = dict(os.environ if environment is None else environment)
    selected_platform = host_platform or _host_platform_target()
    nodes = {item.revision.identity.uri: item for item in snapshot.authority.lock.nodes}
    authorings = {item.identity.uri: item for item in snapshot.authority.authorings}
    node_profiles: dict[
        str,
        tuple[
            StandardLanguageCommandProfile,
            StandardPlatformCommandProfile,
            StandardBuildSystemCommandProfile
            | StandardRepoManCommandProfile
            | StandardMakeCommandProfile
            | StandardCMakeCommandProfile
            | StandardCargoCommandProfile
            | None,
            StandardAcceleratorCommandProfile | None,
            StandardNpmCommandProfile | None,
            ContentIdentity | None,
            StandardPythonWheelCommandProfile | None,
            ContentIdentity | None,
        ],
    ] = {}
    constraints: dict[str, list[ToolchainConstraint]] = {}
    required_tool_names: set[str] = set()
    for plan in execution_plan.generation_plans:
        node = nodes[plan.component_revision.uri]
        authoring = authorings[node.revision.authoring_identity.uri]
        if not authoring.entrypoints and authoring.resolved_kind != "library":
            raise StandardCommandProjectionError(
                "standard_command.zero_entrypoint_kind_unsupported",
                "a zero-entrypoint Standard command Component must declare "
                "kind: library",
            )
        profiles = _profile_contributions(snapshot, node)
        language = _one_profile(
            profiles,
            StandardLanguageCommandProfile,
            required=True,
            label="language command",
        )
        if plan.component_revision.uri in sdk_runtime_revisions and (
            language.runtime_strategy is not StandardLanguageRuntimeStrategy.PYTHON
        ):
            raise StandardCommandProjectionError(
                "standard_command.native_sdk_language_unsupported",
                "native SDK commands require a Python consumer runtime",
            )
        platform = _one_profile(
            profiles,
            StandardPlatformCommandProfile,
            required=True,
            label="platform command",
        )
        build_system = _one_profile(
            profiles,
            (
                StandardBuildSystemCommandProfile,
                StandardRepoManCommandProfile,
                StandardMakeCommandProfile,
                StandardCMakeCommandProfile,
                StandardCargoCommandProfile,
            ),
            required=False,
            label="build-system command",
        )
        accelerator = _one_profile(
            profiles,
            StandardAcceleratorCommandProfile,
            required=False,
            label="accelerator command",
        )
        npm_profile = _one_profile(
            profiles,
            StandardNpmCommandProfile,
            required=False,
            label="npm packaging command",
        )
        python_profile = _one_profile(
            profiles,
            StandardPythonWheelCommandProfile,
            required=False,
            label="Python wheel packaging command",
        )
        python_flavor_revision_identity = (
            None
            if python_profile is None
            else _selected_flavor_revision_identity(
                snapshot, node, axis=FlavorAxis.PACKAGING, target=python_profile.target
            )
        )
        if python_profile is not None and (
            language.target != "python"
            or language.build_strategy is not StandardLanguageBuildStrategy.PYTHON_TREE
            or language.runtime_strategy is not StandardLanguageRuntimeStrategy.PYTHON
            or language.artifact_layout is not StandardArtifactLayout.TREE
            or build_system is not None
            or npm_profile is not None
            or len(authoring.entrypoints) != 1
            or (
                accelerator is not None
                and language.target in accelerator.applies_to_languages
            )
        ):
            raise StandardCommandProjectionError(
                "standard_command.python_wheel_profile_unsupported",
                "Python wheels require one Python application entrypoint without a "
                "competing build-system, compiler or packaging profile",
            )
        if not authoring.entrypoints and npm_profile is not None:
            raise StandardCommandProjectionError(
                "standard_command.library_npm_unsupported",
                "the first importable JavaScript library contract is dependency-free; "
                "package-npm library graphs require separate package-manager authority",
            )
        if npm_profile is not None and language.target != "javascript":
            raise StandardCommandProjectionError(
                "standard_command.npm_language_unsupported",
                "the npm lifecycle requires the JavaScript language Flavor",
            )
        if npm_profile is not None and build_system is not None:
            raise StandardCommandProjectionError(
                "standard_command.npm_build_system_unsupported",
                "package-npm cannot be combined with an explicit Standard "
                "build-system profile until a combined authority is defined",
            )
        npm_flavor_revision_identity = (
            None
            if npm_profile is None
            else _selected_flavor_revision_identity(
                snapshot,
                node,
                axis=FlavorAxis.PACKAGING,
                target=npm_profile.target,
            )
        )
        if platform.target != selected_platform:
            raise StandardCommandProjectionError(
                "standard_command.platform_mismatch",
                f"locked platform {platform.target!r} does not match host "
                f"{selected_platform!r}",
            )
        node_profiles[plan.component_revision.uri] = (
            language,
            platform,
            build_system,
            accelerator,
            npm_profile,
            npm_flavor_revision_identity,
            python_profile,
            python_flavor_revision_identity,
        )
        if (
            accelerator is not None
            and language.target in accelerator.applies_to_languages
        ):
            required_tool_names.add(accelerator.toolchain)
        else:
            required_tool_names.add(language.toolchain)
        if build_system is not None and not isinstance(
            build_system, StandardRepoManCommandProfile
        ):
            required_tool_names.add(build_system.toolchain)
            if isinstance(
                build_system, (StandardMakeCommandProfile, StandardCMakeCommandProfile)
            ):
                required_tool_names.add("python")
        elif npm_profile is None:
            required_tool_names.add("python")
        if npm_profile is not None:
            required_tool_names.add(npm_profile.toolchain)
        if (
            language.runtime_strategy
            is StandardLanguageRuntimeStrategy.NATIVE_EXECUTABLE
        ):
            required_tool_names.add("python")
        for name, constraint in _toolchain_constraints(snapshot, node).items():
            constraints.setdefault(name, []).append(constraint)
    merged_constraints = {
        name: merge_toolchain_constraints(tuple(values))
        for name, values in constraints.items()
    }
    discover = toolchain_discoverer or _discover_toolchain
    tools = {
        name: discover(name, merged_constraints.get(name), configured)
        for name in sorted(required_tool_names - {"npm"})
    }
    if "npm" in required_tool_names:
        node_toolchain = tools.get("node")
        if node_toolchain is None:
            raise StandardCommandProjectionError(
                "standard_command.npm_node_missing",
                "package-npm requires the selected JavaScript Node.js toolchain",
            )
        npm_constraint = merged_constraints.get("npm")
        if npm_constraint is None:
            raise StandardCommandProjectionError(
                "standard_command.npm_constraint_missing",
                "package-npm requires selected Flavor toolchain constraint authority",
            )
        try:
            npm_version_constraint = npm_constraint.version_range()
        except ValueError as exc:
            raise StandardCommandProjectionError(
                "standard_command.npm_constraint_incomplete",
                "package-npm toolchain constraint must declare lower and exclusive "
                "upper bounds",
            ) from exc
        npm_constraint_identity = npm_constraint.identity.uri

        def _discover_selected_npm(node, environment):
            return discover_npm_toolchain(
                node,
                environment,
                version_constraint=npm_version_constraint,
                constraint_identity=npm_constraint_identity,
            )

        npm_discover = npm_toolchain_discoverer or _discover_selected_npm
        try:
            tools["npm"] = npm_discover(node_toolchain, configured)
        except Exception as exc:
            raise StandardCommandProjectionError(
                "standard_command.npm_toolchain_unavailable",
                "supported npm from the selected Node.js installation is unavailable",
            ) from exc
    for name, tool in tools.items():
        if (
            not isinstance(getattr(tool, "command", None), tuple)
            or not isinstance(getattr(tool, "identity", None), str)
            or not callable(getattr(tool, "require_unchanged", None))
        ):
            raise StandardCommandProjectionError(
                "standard_command.toolchain_invalid",
                f"toolchain adapter {name!r} returned incomplete authority",
            )
        tool.require_unchanged()
    if "npm" in tools and (
        getattr(getattr(tools["npm"], "node", None), "identity", None)
        != tools["node"].identity
    ):
        raise StandardCommandProjectionError(
            "standard_command.npm_node_mismatch",
            "selected npm does not belong to the selected Node.js toolchain",
        )
    contracts = []
    bazel_targets = []
    cargo_targets = []
    npm_targets = []
    python_targets = []
    language_by_revision = {}
    single_file_revisions = set()
    for plan in execution_plan.generation_plans:
        node = nodes[plan.component_revision.uri]
        authoring = authorings[node.revision.authoring_identity.uri]
        profiles = node_profiles[plan.component_revision.uri]
        contract, target, language = _command_contract(
            plan,
            authoring,
            node,
            snapshot.authority.lock.target_profile_identity,
            *profiles[:3],
            tools,
            profiles[3],
            profiles[4],
            profiles[5],
            profiles[6],
            profiles[7],
        )
        if plan.component_revision.uri in sdk_runtime_revisions:
            contract = project_native_sdk_commands(
                contract,
                target_identity=node.target_flavor_selection.identity,
            )
        contracts.append(contract)
        language_by_revision[plan.component_revision.uri] = language
        if profiles[2] is not None:
            single_file_revisions.add(plan.component_revision.uri)
        if isinstance(target, StandardBazelTarget):
            bazel_targets.append(target)
        elif isinstance(target, StandardCargoTarget):
            cargo_targets.append(target)
        elif isinstance(target, StandardNpmTarget):
            npm_targets.append(target)
        elif isinstance(target, StandardPythonTarget):
            python_targets.append(target)
    bindings_by_identity = {}
    for contract in contracts:
        for identity in contract.execution_toolchain_identities:
            tool = next(
                item for item in tools.values() if item.identity == identity.uri
            )
            bindings_by_identity.setdefault(
                identity.uri,
                LocalComponentToolBinding.from_observed_toolchain(tool),
            )
    provider_environment = {}
    contracts_by_revision = {item.component_revision.uri: item for item in contracts}
    provider_edges = {
        (
            edge.consumer_revision.uri,
            edge.provider_revision.uri,
            edge.requirement_id,
        ): edge
        for action_plan in execution_plan.action_plans
        for edge in action_plan.dependency_edges
        if edge.semantics.consumed_input is DependencyInputKind.ARTIFACT_EXPORT
    }
    for edge in provider_edges.values():
        provider = contracts_by_revision[edge.provider_revision.uri]
        language = language_by_revision[edge.provider_revision.uri]
        export_id = provider.artifact_export.export_id
        relative = export_id
        if (
            edge.provider_revision.uri not in single_file_revisions
            and language.artifact_layout is StandardArtifactLayout.TREE
        ):
            relative += "/" + language.artifact_entrypoint
        provider_environment[export_id] = (
            "LITAI_PROVIDER_" + edge.provider_revision.digest[:20].upper(),
            relative,
        )
    commands = tuple(tool.command for tool in tools.values())
    if dependency_observer is None:
        with tempfile.TemporaryDirectory(prefix="litai-toolchain-observation-") as root:
            observation = PortableHostDependencyObserver(
                toolchain_commands=commands,
                lifecycle_commands=(),
            ).observe(
                {"artifact_path": root},
                root_ref=(
                    f"urn:literate-ai:component:{execution_plan.root_revision.digest}"
                ),
            )
    else:
        observation = dependency_observer(commands)
    snapshot.require_unchanged()
    for tool in tools.values():
        tool.require_unchanged()
    if native_sdk_inputs is not None:
        native_sdk_generation_identities(native_sdk_inputs, snapshot)
    return project_standard_toolchain_closure(
        execution_plan,
        contracts=tuple(contracts),
        tool_bindings=tuple(
            bindings_by_identity[key] for key in sorted(bindings_by_identity)
        ),
        dependency_observation=observation,
        observer_identity=canonical_identity(
            {
                "schema": "literate-ai/standard-toolchain-observer@1",
                "adapter": "portable-host-dependency-observer@1",
                "platform": selected_platform,
            }
        ),
        bazel_targets=tuple(bazel_targets),
        cargo_targets=tuple(cargo_targets),
        npm_targets=tuple(npm_targets),
        python_targets=tuple(python_targets),
        provider_environment=provider_environment,
        toolchain_authorities=tuple(
            LocalObservedToolchainAuthority.from_observed_toolchain(tools[name])
            for name in sorted(tools)
        ),
    )


def project_standard_toolchain_closure(
    execution_plan: ComponentExecutionPlan,
    *,
    contracts: tuple[ComponentCommandContract, ...],
    tool_bindings: tuple[LocalComponentToolBinding, ...],
    dependency_observation: HostDependencyObservation,
    observer_identity: ContentIdentity,
    bazel_targets: tuple[StandardBazelTarget, ...] = (),
    cargo_targets: tuple[StandardCargoTarget, ...] = (),
    npm_targets: tuple[StandardNpmTarget, ...] = (),
    python_targets: tuple[StandardPythonTarget, ...] = (),
    provider_environment: Mapping[str, tuple[str, str]] | None = None,
    toolchain_authorities: tuple[LocalObservedToolchainAuthority, ...] = (),
) -> ProjectedStandardToolchainClosure:
    """Bind one exact plan to commands, targets, providers, and observed host tools."""

    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    if not isinstance(observer_identity, ContentIdentity):
        raise TypeError("observer_identity must be a ContentIdentity")
    if any(not isinstance(item, ComponentCommandContract) for item in contracts):
        raise TypeError("contracts must contain ComponentCommandContract values")
    if any(not isinstance(item, LocalComponentToolBinding) for item in tool_bindings):
        raise TypeError("tool_bindings must contain LocalComponentToolBinding values")
    if any(not isinstance(item, StandardBazelTarget) for item in bazel_targets):
        raise TypeError("bazel_targets must contain StandardBazelTarget values")
    if any(not isinstance(item, StandardCargoTarget) for item in cargo_targets):
        raise TypeError("cargo_targets must contain StandardCargoTarget values")
    if any(not isinstance(item, StandardNpmTarget) for item in npm_targets):
        raise TypeError("npm_targets must contain StandardNpmTarget values")
    if any(not isinstance(item, StandardPythonTarget) for item in python_targets):
        raise TypeError("python_targets must contain StandardPythonTarget values")
    if any(
        not isinstance(item, LocalObservedToolchainAuthority)
        for item in toolchain_authorities
    ):
        raise TypeError(
            "toolchain_authorities must contain LocalObservedToolchainAuthority values"
        )
    contract_map = {item.component_revision.uri: item for item in contracts}
    planned = {
        item.component_revision.uri: item for item in execution_plan.generation_plans
    }
    if len(contract_map) != len(contracts) or set(contract_map) != set(planned):
        raise ValueError(
            "command contracts must cover every and only planned Component"
        )
    if bazel_targets and cargo_targets:
        raise ValueError(
            "one Standard project cannot mix Bazel and Cargo lifecycle targets"
        )
    all_targets = (*bazel_targets, *cargo_targets, *npm_targets, *python_targets)
    target_map = {item.component_revision.uri: item for item in all_targets}
    if len(target_map) != len(all_targets) or not set(target_map).issubset(planned):
        raise ValueError("build targets must name unique planned Components")
    for revision, target in target_map.items():
        contract = contract_map[revision]
        if (
            contract.locked_build_authority_identity != target.identity
            or contract.build_system_resolver_identity
            != target.build_system_resolver_identity
            or contract.build_system_toolchain_identity
            != target.build_system_toolchain_identity
            or contract.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity
            != target.build_system_toolchain_identity
            or (
                isinstance(target, StandardNpmTarget)
                and (
                    contract.language_compiler_identity
                    != target.node_toolchain_identity
                    or contract.language_runtime_identity
                    != target.node_toolchain_identity
                )
            )
            or (
                isinstance(target, StandardPythonTarget)
                and (
                    contract.language_compiler_identity
                    != target.python_toolchain_identity
                    or contract.language_runtime_identity
                    != target.python_toolchain_identity
                )
            )
        ):
            raise ValueError("build target differs from locked Component authority")

    binding_map = {item.toolchain_identity.uri: item for item in tool_bindings}
    required_command_toolchains = {
        identity.uri
        for contract in contracts
        for identity in contract.execution_toolchain_identities
    }
    if (
        len(binding_map) != len(tool_bindings)
        or set(binding_map) != required_command_toolchains
    ):
        raise ValueError("tool bindings must cover every and only execution toolchain")
    if any(
        binding_map[target.build_system_toolchain_identity.uri].command
        != target.npm_command
        for target in npm_targets
    ):
        raise ValueError("npm target command differs from its locked tool binding")
    if any(
        binding_map[target.python_toolchain_identity.uri].command
        != target.python_command
        for target in python_targets
    ):
        raise ValueError("Python target command differs from its locked tool binding")
    for binding in tool_bindings:
        binding.require_unchanged()
    if not toolchain_authorities:
        toolchain_authorities = tuple(
            LocalObservedToolchainAuthority(
                binding.toolchain_identity, binding.require_unchanged
            )
            for binding in tool_bindings
        )
    authority_map = {item.identity.uri: item for item in toolchain_authorities}
    required_toolchains = {
        identity.uri
        for contract in contracts
        for identity in (
            contract.build_system_toolchain_identity,
            contract.language_compiler_identity,
            contract.language_runtime_identity,
            *(item.toolchain_identity for item in contract.tool_bindings),
        )
    }
    if (
        len(authority_map) != len(toolchain_authorities)
        or set(authority_map) != required_toolchains
    ):
        raise ValueError(
            "observed toolchain authorities must cover build, compiler, runtime, "
            "and command identities"
        )
    for authority in toolchain_authorities:
        authority.require_unchanged()

    provider_environment = dict(provider_environment or {})
    provider_bindings = _provider_bindings(provider_environment)
    required_provider_exports = {
        contract_map[edge.provider_revision.uri].artifact_export.export_id
        for action_plan in execution_plan.action_plans
        for edge in action_plan.dependency_edges
        if edge.semantics.consumed_input is DependencyInputKind.ARTIFACT_EXPORT
    }
    if set(provider_environment) != required_provider_exports:
        raise ValueError(
            "provider bindings must cover every and only build dependency export; "
            f"expected {sorted(required_provider_exports)!r}, got "
            f"{sorted(provider_environment)!r}"
        )

    authorities = tuple(
        StandardComponentCommandAuthority(
            component_revision=plan.component_revision,
            generation_key_identity=plan.generation_key.identity,
            flavor_selection_identity=plan.generation_key.flavor_selection_identity,
            command_contract_identity=contract_map[
                plan.component_revision.uri
            ].identity,
            build_target_identity=(
                None
                if plan.component_revision.uri not in target_map
                else target_map[plan.component_revision.uri].identity
            ),
        )
        for plan in execution_plan.generation_plans
    )
    record = StandardToolchainClosure(
        component_lock_identity=execution_plan.component_lock_identity,
        execution_plan_identity=execution_plan.identity,
        component_authorities=authorities,
        toolchain_identities=tuple(
            sorted(
                (item.identity for item in toolchain_authorities),
                key=lambda item: item.uri,
            )
        ),
        provider_bindings=provider_bindings,
        dependency_graph_identity=_dependency_observation_identity(
            dependency_observation
        ),
        observer_identity=observer_identity,
    )
    return ProjectedStandardToolchainClosure(
        record,
        contracts,
        tool_bindings,
        toolchain_authorities,
        bazel_targets,
        cargo_targets,
        npm_targets,
        provider_environment,
        dependency_observation,
        python_targets,
    )


@dataclass(frozen=True, slots=True)
class StandardProjectExecutionRequest:
    """Complete caller-owned inputs for one locked Standard project execution."""

    planned: PlannedStandardProject
    source_root: Path
    invalidation: ComponentInvalidationDecision
    source_cache_memberships: (
        Mapping[str, StandardSourceCacheMembership | StandardSourceAdmissionMembership]
        | None
    ) = None
    source_generation_resume_candidates: (
        Mapping[str, SourceGenerationResumeCandidate] | None
    ) = None
    resume_candidates: Mapping[str, StandardNodeAcceptedCandidate] | None = None
    max_parallelism: int = 1
    budget: GenerationComplexityBudget | None = None
    accepted_source_only: bool = False
    expected_framework_distribution_identity: ContentIdentity | None = None
    worker_source_selectors: StandardSourceSelectorSet | None = None
    directory_custody_identity: ContentIdentity | None = None
    fresh_source: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.planned, PlannedStandardProject):
            raise TypeError("planned must be a PlannedStandardProject")
        if not isinstance(self.source_root, Path):
            raise TypeError("source_root must be a Path")
        if not isinstance(self.invalidation, ComponentInvalidationDecision):
            raise TypeError("invalidation must be a ComponentInvalidationDecision")
        if not isinstance(self.max_parallelism, int) or self.max_parallelism < 1:
            raise ValueError("max_parallelism must be a positive integer")
        if type(self.accepted_source_only) is not bool:
            raise TypeError("accepted_source_only must be a bool")
        if type(self.fresh_source) is not bool:
            raise TypeError("fresh_source must be a bool")
        if self.fresh_source and (
            self.accepted_source_only
            or self.source_cache_memberships
            or self.source_generation_resume_candidates
            or self.resume_candidates
        ):
            raise ValueError(
                "fresh source cannot use prior source or lifecycle evidence"
            )
        if self.directory_custody_identity is not None and not isinstance(
            self.directory_custody_identity, ContentIdentity
        ):
            raise TypeError("directory_custody_identity must be a ContentIdentity")
        if self.accepted_source_only and (
            not isinstance(
                self.expected_framework_distribution_identity, ContentIdentity
            )
            or not isinstance(self.worker_source_selectors, StandardSourceSelectorSet)
            or not isinstance(self.directory_custody_identity, ContentIdentity)
        ):
            raise TypeError(
                "accepted_source_only requires framework, worker-selector, and "
                "directory-custody identities"
            )


class PreparedStandardSourceCacheRestorer(Protocol):
    """Materialize and register an exact cache hit for one prepared node."""

    def restore_prepared(
        self,
        execution_plan: ComponentExecutionPlan,
        prepared: PreparedComponentGenerationNode[object, object],
        *,
        force_regeneration: bool = False,
    ) -> StandardSourceCacheMembership | None: ...


class PreparedStandardLifecycleCheckpointStore(Protocol):
    """Start checkpoint attempts, restore source, and receive typed boundaries."""

    def start_attempt(
        self,
        execution_plan: ComponentExecutionPlan,
        prepared_nodes: Mapping[str, PreparedComponentGenerationNode[object, object]],
    ) -> None: ...

    def restore_prepared(
        self,
        execution_plan: ComponentExecutionPlan,
        prepared: PreparedComponentGenerationNode[object, object],
    ) -> SourceGenerationResumeCandidate | None: ...

    def record(self, evidence: StandardLifecycleStageEvidence) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutedStandardProject:
    """Prepared custody, lifecycle result, and local build-cache evidence."""

    planned: PlannedStandardProject
    prepared: PreparedExecutableProject[object, object]
    lifecycle: StandardProjectLifecycleResult
    local_build_cache_report: Mapping[str, int | float]


@dataclass(frozen=True, slots=True)
class StandardProjectPublicationRequest:
    """Caller-owned policy, destination, and custody inputs for explicit release."""

    service: StandardProjectReleaseService
    component_lock: ComponentLock
    declarations: tuple[StandardReleaseDeclaration, ...]
    root_source_bundle: SourceBundleClosure
    evidence_manifest: BlobRef
    read_blob: ReleaseBlobReader
    security_classification_identity: ContentIdentity
    security_profile: SecurityProfile
    actor: str
    reason: str
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReleasedStandardProject:
    """One accepted execution and its explicitly requested publication result."""

    execution: ExecutedStandardProject
    release: StandardProjectReleaseResult


@dataclass(frozen=True, slots=True)
class FilesystemStandardProjectRuntime:
    """Explicit component-scoped host composition root; not a generic CLI runtime."""

    planning: FilesystemStandardProjectPlanningAdapter
    node_preparation: LockedComponentNodePreparationAdapter
    source_trees: LocalSourceTreeRegistry
    lifecycle_ports: LocalStandardLifecyclePorts
    application: StandardProjectApplicationService
    indexer_configured: bool
    durable_source_cache_configured: bool
    toolchain_closure: ProjectedStandardToolchainClosure
    source_cache_restorer: PreparedStandardSourceCacheRestorer | None
    checkpoint_store: PreparedStandardLifecycleCheckpointStore | None
    context_evidence_recorder: (
        FilesystemForwardGenerationContextEvidenceRecorder | None
    ) = None

    def plan(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        *,
        assets: tuple[AuthoredBinaryAsset, ...] | None = None,
    ) -> PlannedStandardProject:
        return self.planning.plan(snapshot, assets=assets)

    def prepare(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        planned: PlannedStandardProject,
        *,
        source_root: Path,
        budget: GenerationComplexityBudget | None = None,
    ) -> PreparedExecutableProject[object, object]:
        """Prepare every locked node into a distinct source-custody workspace."""

        if not isinstance(planned, PlannedStandardProject):
            raise TypeError("planned must be a PlannedStandardProject")
        if source_root.is_symlink():
            raise ValueError("Standard source runtime root cannot be a symlink")
        root = source_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        allocator = FilesystemComponentWorkspaceAllocator(root)
        return StandardProjectApplicationService.prepare(
            planned.execution_plan,
            authority=snapshot,
            authority_lock_identity=self.node_preparation.authority_lock_identity,
            authority_guard=self.node_preparation.guard,
            node_projector=self.node_preparation.project,
            workspace_allocator=allocator.allocate,
            framework_envelope=lambda projection: projection.recipe.prompt(
                include_locked_authority_documents=False
            ).encode("utf-8"),
            budget=budget or _default_generation_budget(),
        )

    def execute(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        request: StandardProjectExecutionRequest,
    ) -> ExecutedStandardProject:
        """Prepare and execute one exact plan without exposing lifecycle assembly."""

        if not hasattr(snapshot, "authority") or not callable(
            getattr(snapshot, "require_unchanged", None)
        ):
            raise TypeError("snapshot must provide locked generation authority")
        if not isinstance(request, StandardProjectExecutionRequest):
            raise TypeError("request must be a StandardProjectExecutionRequest")
        snapshot.require_unchanged()
        request.planned.coding_cli.require_unchanged()
        if (
            request.planned.execution_plan.component_lock_identity
            != snapshot.authority.lock.identity
        ):
            raise ValueError("planned execution does not belong to the locked snapshot")
        prepared = self.prepare(
            snapshot,
            request.planned,
            source_root=request.source_root,
            budget=request.budget,
        )
        if self.context_evidence_recorder is not None:
            self.context_evidence_recorder.start_attempt()
        cache_memberships = dict(request.source_cache_memberships or {})
        source_resumes = dict(request.source_generation_resume_candidates or {})
        if self.checkpoint_store is not None:
            self.checkpoint_store.start_attempt(
                request.planned.execution_plan, prepared.nodes_by_revision
            )
        if self.source_cache_restorer is not None and not request.fresh_source:
            regenerate = {item.uri for item in request.invalidation.regenerate}
            for uri, node in prepared.nodes_by_revision.items():
                if uri in cache_memberships or uri in regenerate:
                    continue
                restored = self.source_cache_restorer.restore_prepared(
                    request.planned.execution_plan,
                    node,
                )
                if restored is not None:
                    cache_memberships[uri] = restored
        if request.accepted_source_only:
            required = set(prepared.nodes_by_revision)
            admitted = {
                uri
                for uri, membership in cache_memberships.items()
                if isinstance(membership, StandardSourceAdmissionMembership)
            }
            if admitted != required:
                missing = sorted(required - admitted)
                raise StandardAcceptedSourceContinuationError(
                    "source_cache.runtime_absent",
                    "accepted-source continuation requires one exact verifier-admitted "
                    "cache member for every planned Component"
                    + (f": {', '.join(missing)}" if missing else ""),
                )
            assert request.expected_framework_distribution_identity is not None
            assert request.worker_source_selectors is not None
            for uri in sorted(required):
                node = prepared.nodes_by_revision[uri]
                membership = cache_memberships[uri]
                assert isinstance(membership, StandardSourceAdmissionMembership)
                try:
                    require_source_admission_for_worker(
                        membership,
                        expected_component_lock_identity=(
                            request.planned.execution_plan.component_lock_identity
                        ),
                        expected_generation_plan_identity=node.plan.identity,
                        expected_generation_key_identity=(
                            node.plan.generation_key.identity
                        ),
                        expected_flavor_set_identity=(
                            node.plan.generation_key.flavor_selection_identity
                        ),
                        expected_skill_closure_identity=canonical_identity(
                            {
                                "schema": "literate-ai/generation-skill-closure@1",
                                "skill_identities": [
                                    item.uri
                                    for item in (
                                        node.plan.generation_key.skill_identities
                                    )
                                ],
                            }
                        ),
                        expected_framework_distribution_identity=(
                            request.expected_framework_distribution_identity
                        ),
                        worker_selectors=request.worker_source_selectors,
                    )
                except StandardSourceAdmissionError as exc:
                    raise StandardAcceptedSourceContinuationError(
                        exc.code, exc.message
                    ) from exc
            source_resumes.clear()
        if self.checkpoint_store is not None and not request.fresh_source:
            regenerate = {item.uri for item in request.invalidation.regenerate}
            for uri, node in prepared.nodes_by_revision.items():
                if (
                    uri in cache_memberships
                    or uri in source_resumes
                    or uri in regenerate
                ):
                    continue
                restored = self.checkpoint_store.restore_prepared(
                    request.planned.execution_plan,
                    node,
                )
                if restored is not None:
                    generator = self.application.lifecycle.generator
                    record_cache_key = getattr(
                        generator, "record_restored_cache_key", None
                    )
                    planned_cache_key = getattr(generator, "planned_cache_key", None)
                    if callable(record_cache_key) and callable(planned_cache_key):
                        record_cache_key(
                            restored.output.candidate,
                            planned_cache_key(node),
                        )
                    source_resumes[uri] = restored
        if source_resumes:
            lifecycle = self.application.rebuild(
                prepared,
                component_lock=snapshot.authority.lock,
                invalidation=request.invalidation,
                source_cache_memberships=cache_memberships or None,
                source_generation_resume_candidates=source_resumes,
                resume_candidates=request.resume_candidates,
                max_parallelism=request.max_parallelism,
            )
        else:
            lifecycle = self.application.rebuild(
                prepared,
                component_lock=snapshot.authority.lock,
                invalidation=request.invalidation,
                source_cache_memberships=cache_memberships or None,
                resume_candidates=request.resume_candidates,
                max_parallelism=request.max_parallelism,
            )
        snapshot.require_unchanged()
        request.planned.coding_cli.require_unchanged()
        return ExecutedStandardProject(
            request.planned,
            prepared,
            lifecycle,
            {
                "hits": self.lifecycle_ports.build_cache_hits,
                "misses": self.lifecycle_ports.build_cache_misses,
                "hit_seconds": self.lifecycle_ports.build_cache_hit_seconds,
                "build_seconds": self.lifecycle_ports.build_seconds,
            },
        )

    def execute_and_publish(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        execution_request: StandardProjectExecutionRequest,
        publication_request: StandardProjectPublicationRequest,
    ) -> ReleasedStandardProject:
        """Execute, then package and publish only through explicit caller authority."""

        if not isinstance(publication_request, StandardProjectPublicationRequest):
            raise TypeError(
                "publication_request must be a StandardProjectPublicationRequest"
            )
        if not isinstance(execution_request, StandardProjectExecutionRequest):
            raise TypeError(
                "execution_request must be a StandardProjectExecutionRequest"
            )
        snapshot.require_unchanged()
        if publication_request.component_lock != snapshot.authority.lock:
            raise ValueError("publication Component lock differs from locked snapshot")
        if (
            execution_request.planned.execution_plan.component_lock_identity
            != publication_request.component_lock.identity
        ):
            raise ValueError("publication Component lock differs from execution plan")
        executed = self.execute(snapshot, execution_request)
        released = publication_request.service.publish_accepted(
            execution_request.planned.execution_plan,
            executed.lifecycle,
            publication_request.component_lock,
            publication_request.declarations,
            root_source_bundle=publication_request.root_source_bundle,
            evidence_manifest=publication_request.evidence_manifest,
            read_blob=publication_request.read_blob,
            security_classification_identity=(
                publication_request.security_classification_identity
            ),
            security_profile=publication_request.security_profile,
            actor=publication_request.actor,
            reason=publication_request.reason,
            now=publication_request.now,
        )
        if released.lifecycle is not executed.lifecycle:
            raise ValueError("release result does not retain the accepted lifecycle")
        snapshot.require_unchanged()
        execution_request.planned.coding_cli.require_unchanged()
        return ReleasedStandardProject(executed, released)

    def production_readiness(
        self, planned: PlannedStandardProject
    ) -> StandardProjectRuntimeReadiness:
        """Enumerate capabilities still required before production execution."""

        if not isinstance(planned, PlannedStandardProject):
            raise TypeError("planned must be a PlannedStandardProject")
        planned_revisions = {
            item.component_revision.uri
            for item in planned.execution_plan.generation_plans
        }
        configured_revisions = set(self.lifecycle_ports.contracts)
        # Strict post-source and aggregate receipt evidence is produced by the
        # application core. Readiness tracks only capabilities that composition must
        # still supply around that core.
        blockers: set[str] = set()
        if configured_revisions != planned_revisions:
            blockers.add("component-command-contract-coverage-incomplete")
        if not self.lifecycle_ports.locked_command_authority_is_current():
            blockers.add("component-command-tool-binding-invalid")
        try:
            self.toolchain_closure.require_unchanged()
        except (OSError, RuntimeError, ValueError):
            blockers.add("toolchain-closure-invalid")
        if (
            self.toolchain_closure.record.execution_plan_identity
            != planned.execution_plan.identity
            or self.toolchain_closure.record.component_lock_identity
            != planned.execution_plan.component_lock_identity
        ):
            blockers.add("toolchain-closure-plan-mismatch")
        if not self.indexer_configured:
            blockers.add("indexer-unconfigured")
        if not self.durable_source_cache_configured:
            blockers.add("source-cache-round-trip-unavailable")
        if self.checkpoint_store is None:
            blockers.add("stage-checkpoint-round-trip-unavailable")
        ordered = tuple(sorted(blockers))
        return StandardProjectRuntimeReadiness(not ordered, ordered)


def assemble_filesystem_standard_project_runtime(
    *,
    generator: ComponentSourceGenerationRunner,
    object_root: Path,
    toolchain_closure: ProjectedStandardToolchainClosure,
    python_wheelhouse: Path | None = None,
    planning: FilesystemStandardProjectPlanningAdapter | None = None,
    node_preparation: LockedComponentNodePreparationAdapter | None = None,
    source_trees: LocalSourceTreeRegistry | None = None,
    indexer: GenerationIndexer | None = None,
    source_cache_publisher: AcceptedSourceCachePublisher | None = None,
    source_cache_restorer: PreparedStandardSourceCacheRestorer | None = None,
    checkpoint_store: PreparedStandardLifecycleCheckpointStore | None = None,
    independent_acceptance_oracle: object | None = None,
    browser_driver: object | None = None,
    native_sdk_inputs=None,
    bazel_cache_arguments: tuple[str, ...] = (),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FilesystemStandardProjectRuntime:
    """Wire an explicit component-scoped host runtime, never a default CLI claim."""

    if not isinstance(toolchain_closure, ProjectedStandardToolchainClosure):
        raise TypeError(
            "public Standard runtime requires a projected toolchain closure"
        )
    toolchain_closure.require_unchanged()
    if toolchain_closure.python_targets and python_wheelhouse is None:
        raise StandardCommandProjectionError(
            "standard_command.python_wheelhouse_missing",
            "Python wheel builds require an explicit provisioned wheel directory; "
            "automatic network acquisition is not configured",
        )
    contracts = toolchain_closure.contracts
    tool_bindings = toolchain_closure.tool_bindings
    provider_environment = toolchain_closure.provider_environment

    registry = source_trees or LocalSourceTreeRegistry()
    planned_cache_key = getattr(generator, "planned_cache_key", None)

    def context_cache_key(
        prepared: PreparedComponentGenerationNode[object, object],
    ) -> ContentIdentity:
        if callable(planned_cache_key):
            value = planned_cache_key(prepared)
            identity = getattr(value, "identity", value)
            if not isinstance(identity, ContentIdentity):
                raise TypeError(
                    "planned source cache key must expose a ContentIdentity"
                )
            return identity
        request = prepared.request.request
        return canonical_identity(
            {
                "schema": "literate-ai/forward-generation-context-cache-key@1",
                "component_revision": prepared.plan.component_revision.uri,
                "generation_plan_identity": prepared.plan.identity.uri,
                "generation_key_identity": prepared.plan.generation_key.identity.uri,
                "bounded_request_identity": request.identity.uri,
                "context_manifest_identity": request.context_manifest_identity.uri,
                "complexity_budget_identity": request.budget.identity.uri,
                "complexity_decision_identity": (
                    request.complexity_decision_identity.uri
                ),
            }
        )

    context_evidence_recorder = FilesystemForwardGenerationContextEvidenceRecorder(
        FilesystemForwardGenerationContextJournal(
            object_root / ".litai" / "forward-generation-context-cas"
        ),
        cache_key=context_cache_key,
    )
    port_arguments = {
        "source_trees": registry,
        "object_root": object_root,
        "contracts": contracts,
        "tool_bindings": tool_bindings,
        "npm_targets": toolchain_closure.npm_targets,
        "python_targets": toolchain_closure.python_targets,
        "python_wheelhouse": python_wheelhouse,
        "provider_environment": provider_environment,
        "dependency_observation": toolchain_closure.dependency_observation,
        "independent_acceptance_oracle": independent_acceptance_oracle,
        "browser_driver": browser_driver,
        "native_sdk_inputs": native_sdk_inputs,
        "clock": clock,
    }
    if toolchain_closure.bazel_targets:
        ports = StandardBazelLifecyclePorts(
            **port_arguments,
            bazel_targets=toolchain_closure.bazel_targets,
            bazel_cache_arguments=bazel_cache_arguments,
        )
    elif toolchain_closure.cargo_targets:
        ports = StandardCargoLifecyclePorts(
            **port_arguments,
            cargo_targets=toolchain_closure.cargo_targets,
        )
    else:
        ports = LocalStandardLifecyclePorts(**port_arguments)
    from literate_ai.adapters.candidate_repair import (
        FilesystemStandardCandidateRepairAdapter,
    )

    application = assemble_standard_project_application_service(
        ports=ports,
        generator=RegisteredSourceGenerationRunner(generator, registry),
        indexer=indexer,
        source_cache_publisher=source_cache_publisher,
        checkpoint_recorder=checkpoint_store,
        context_evidence_recorder=context_evidence_recorder,
        candidate_repair_port=FilesystemStandardCandidateRepairAdapter(
            ports.failure_diagnostics
        ),
        clock=clock,
    )
    return FilesystemStandardProjectRuntime(
        planning
        or FilesystemStandardProjectPlanningAdapter(
            native_sdk_inputs=native_sdk_inputs
        ),
        node_preparation
        or LockedComponentNodePreparationAdapter(native_sdk_inputs=native_sdk_inputs),
        registry,
        ports,
        application,
        indexer is not None,
        callable(getattr(source_cache_publisher, "publish_accepted", None))
        and source_cache_restorer is not None,
        toolchain_closure,
        source_cache_restorer,
        checkpoint_store,
        context_evidence_recorder,
    )


def compose_filesystem_standard_source_cache(
    runtime: FilesystemStandardProjectRuntime,
    *,
    generator: ComponentSourceGenerationRunner,
    resolver: SourceCacheResolver,
    caller_cas: FileSystemCAS,
    indexer: GenerationIndexer,
    materializer: SourceCacheMaterializer | None = None,
) -> FilesystemStandardProjectRuntime:
    """Attach complete durable Standard cache custody to an assembled host runtime."""

    planned_key = getattr(generator, "planned_cache_key", None)
    candidate_key = getattr(generator, "cache_key_for_candidate", None)
    restored_key_recorder = getattr(generator, "record_restored_cache_key", None)
    if not callable(planned_key) or not callable(candidate_key):
        raise TypeError(
            "durable Standard cache requires predictable and captured key providers"
        )
    if not isinstance(resolver, SourceCacheResolver):
        raise TypeError("resolver must be a SourceCacheResolver")
    if not isinstance(caller_cas, FileSystemCAS):
        raise TypeError("caller_cas must be a FileSystemCAS")
    if not callable(getattr(indexer, "index", None)):
        raise TypeError("indexer must provide index")
    selected_materializer = materializer or SourceCacheMaterializer()
    publisher = FilesystemStandardAcceptedSourcePublisher(
        cache=resolver,
        caller_cas=caller_cas,
        source_root=runtime.source_trees.resolve,
        source_custody=runtime.source_trees.evidence,
        resolved_sbom_content=lambda publication: (
            runtime.lifecycle_ports.resolved_sbom_content(publication.build_evidence)
        ),
        cache_key=lambda publication: candidate_key(
            publication.source_output.candidate
        ),
    )
    restorer = FilesystemStandardSourceRestorer(
        resolver=resolver,
        materializer=selected_materializer,
        cache_key=planned_key,
        source_trees=runtime.source_trees,
        cache_key_recorder=(
            restored_key_recorder if callable(restored_key_recorder) else None
        ),
    )
    application = assemble_standard_project_application_service(
        ports=runtime.lifecycle_ports,
        generator=RegisteredSourceGenerationRunner(generator, runtime.source_trees),
        indexer=indexer,
        source_cache_publisher=publisher,
        checkpoint_recorder=runtime.checkpoint_store,
        context_evidence_recorder=runtime.context_evidence_recorder,
        clock=runtime.application.lifecycle.clock,
    )
    return replace(
        runtime,
        application=application,
        indexer_configured=True,
        durable_source_cache_configured=True,
        source_cache_restorer=restorer,
    )


def compose_filesystem_standard_lifecycle_checkpoints(
    runtime: FilesystemStandardProjectRuntime,
    *,
    checkpoint_root: Path,
) -> FilesystemStandardProjectRuntime:
    """Attach durable stage lineage and restart-safe source restoration."""

    store = FilesystemStandardLifecycleCheckpointStore(
        checkpoint_root,
        source_root=runtime.source_trees.resolve,
        source_trees=runtime.source_trees,
    )
    lifecycle = runtime.application.lifecycle
    application = assemble_standard_project_application_service(
        ports=runtime.lifecycle_ports,
        generator=lifecycle.generator,
        indexer=lifecycle.indexer,
        source_cache_publisher=lifecycle.source_cache_publisher,
        checkpoint_recorder=store,
        context_evidence_recorder=runtime.context_evidence_recorder,
        clock=lifecycle.clock,
    )
    return replace(runtime, application=application, checkpoint_store=store)


def _default_generation_budget() -> GenerationComplexityBudget:
    return GenerationComplexityBudget(
        max_prompt_bytes=1_000_000,
        max_estimated_tokens=250_000,
        max_document_count=100,
        max_direct_interface_bytes=100_000,
        max_dependency_fan_in=32,
        max_model_attempts=3,
        max_wall_time_ms=900_000,
        max_model_tokens=250_000,
        max_cost_microunits=100_000_000,
    )


@dataclass(frozen=True, slots=True)
class GeneratedStandardSourceAdmissionCandidate:
    """Exact source-only generation custody needed by verifier admission."""

    generation: SourceGenerationResumeCandidate
    cache_key: SourceDerivationCacheKey
    source_root: Path
    flavor_set_identity: ContentIdentity
    skill_closure_identity: ContentIdentity
    coding_cli_transcript_identity: ContentIdentity
    inherited_session_handoff: InheritedSessionHandoffEvidence | None = None


@dataclass(frozen=True, slots=True)
class GeneratedStandardProject:
    coding_cli: CodingCliSelection | InheritedSessionSelection
    custody: ProjectSourceGenerationCustody
    local_cache_report: dict[str, object]
    context_prompt_journal_identities: tuple[ContentIdentity, ...] = ()
    context_benchmarks: tuple[ComponentContextBenchmarkRecord, ...] = ()
    context_cache_report: ForwardGenerationContextCacheReport | None = None
    admission_candidates: tuple[GeneratedStandardSourceAdmissionCandidate, ...] = ()


CODING_CLI_GENERATION_TIMEOUT_ENVIRONMENT = "LITERATE_AI_CODING_CLI_TIMEOUT_SECONDS"
_DEFAULT_CODING_CLI_GENERATION_TIMEOUT_SECONDS = 900
CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT = (
    "LITERATE_AI_CODING_CLI_GENERATION_STDERR_LIMIT_BYTES"
)
_MAXIMUM_CODING_CLI_GENERATION_STDERR_LIMIT_BYTES = 256 * 1024 * 1024


def _coding_cli_generation_timeout_seconds() -> int:
    """Read the local generation timeout override, defaulting to 900 seconds.

    Neither the remote-worker dispatch timeout (--worker-timeout-seconds,
    only meaningful with --worker) nor any prior configuration reached this
    constructor, so a Component whose spec legitimately needs more than 15
    minutes of coding-CLI generation time could not be built locally at all
    (see issue #34).
    """

    configured = os.environ.get(CODING_CLI_GENERATION_TIMEOUT_ENVIRONMENT)
    if configured is None:
        return _DEFAULT_CODING_CLI_GENERATION_TIMEOUT_SECONDS
    try:
        value = int(configured)
    except ValueError:
        value = 0
    if value < 1:
        raise CodingCliError(
            "coding_cli.generation_timeout_configuration_invalid",
            f"{CODING_CLI_GENERATION_TIMEOUT_ENVIRONMENT} must be a positive integer",
        )
    return value


def _coding_cli_generation_stderr_limit_bytes() -> int:
    """Read the finite local source-generation stderr allowance."""

    configured = os.environ.get(CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT)
    if configured is None:
        return DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES
    try:
        value = int(configured)
    except ValueError:
        value = 0
    if not 1 <= value <= _MAXIMUM_CODING_CLI_GENERATION_STDERR_LIMIT_BYTES:
        raise CodingCliError(
            "coding_cli.generation_stderr_limit_configuration_invalid",
            f"{CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT} must be an integer "
            f"between 1 and {_MAXIMUM_CODING_CLI_GENERATION_STDERR_LIMIT_BYTES}",
        )
    return value


class FilesystemStandardSourceGenerationAdapter:
    """Prepare and run a locked source-only Standard project on the local host."""

    def __init__(
        self,
        *,
        generator: CodingCliSourceGenerator,
        project_root: Path,
        cache_root: Path,
        cas_root: Path,
        budget: GenerationComplexityBudget | None = None,
        model_selector: LockedComponentModelSelectionAdapter | None = None,
        project_authority_identity: ContentIdentity | None = None,
        native_sdk_inputs=None,
    ) -> None:
        if not isinstance(generator, CodingCliSourceGenerator):
            raise TypeError("generator must be a CodingCliSourceGenerator")
        if (
            generator.source_intelligence_provider is not None
            or generator.source_intelligence_mode != "off"
        ):
            raise ValueError("source-only generation requires indexing to be off")
        self.generator = generator
        self.project_root = project_root.resolve(strict=True)
        self.cache_root = cache_root
        self.cas_root = cas_root
        self.budget = budget or _default_generation_budget()
        self.model_selector = model_selector or LockedComponentModelSelectionAdapter()
        if project_authority_identity is not None and not isinstance(
            project_authority_identity, ContentIdentity
        ):
            raise TypeError(
                "project_authority_identity must be a ContentIdentity or null"
            )
        self.project_authority_identity = project_authority_identity
        self.native_sdk_inputs = native_sdk_inputs

    @classmethod
    def from_environment(
        cls,
        *,
        project_root: Path,
        cache_root: Path,
        cas_root: Path,
        budget: GenerationComplexityBudget | None = None,
        pipeline_model: str | None = None,
        accepted_source_provider_id: str | None = None,
        accepted_source_provider_identity: ContentIdentity | None = None,
        project_authority_identity: ContentIdentity | None = None,
        native_sdk_inputs=None,
    ) -> FilesystemStandardSourceGenerationAdapter:
        """Select and guard the host coding CLI behind the filesystem boundary."""

        if (accepted_source_provider_id is None) != (
            accepted_source_provider_identity is None
        ):
            raise TypeError(
                "accepted-source provider ID and identity must be supplied together"
            )
        if accepted_source_provider_identity is not None and not isinstance(
            accepted_source_provider_identity, ContentIdentity
        ):
            raise TypeError(
                "accepted_source_provider_identity must be a ContentIdentity or null"
            )
        generator = (
            CodingCliSourceGenerator.for_accepted_source_lookup(
                accepted_source_provider_id,
                accepted_source_provider_identity,
            )
            if accepted_source_provider_identity is not None
            else (
                InheritedSessionSourceGenerator.from_environment()
                if selected_coding_provider() == "inherited-session"
                else CodingCliSourceGenerator(
                    timeout_seconds=_coding_cli_generation_timeout_seconds(),
                    maximum_cli_stderr_bytes=(
                        _coding_cli_generation_stderr_limit_bytes()
                    ),
                    source_intelligence_provider=None,
                    source_intelligence_mode="off",
                    use_default_source_intelligence_provider=False,
                )
            )
        )
        return cls(
            generator=generator,
            project_root=project_root,
            cache_root=cache_root,
            cas_root=cas_root,
            budget=budget,
            model_selector=LockedComponentModelSelectionAdapter(
                pipeline_model=pipeline_model
            ),
            project_authority_identity=project_authority_identity,
            native_sdk_inputs=native_sdk_inputs,
        )

    def generate(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        *,
        output_root: Path,
        max_parallelism: int = 1,
    ) -> GeneratedStandardProject:
        output = output_root.resolve()
        if output_root.is_symlink() or (
            output.exists() and (not output.is_dir() or any(output.iterdir()))
        ):
            raise ValueError("source custody root must be new or empty")
        output.mkdir(parents=True, exist_ok=True)
        # The generated-source cache owns the shared BUILD_DIR marker.  Establish
        # that ownership before the candidate CAS creates a sibling subtree; doing
        # this in the opposite order leaves a legitimate fresh cache non-empty but
        # unmarked, which the cache safety boundary must reject.
        ensure_cache_directory(
            self.cache_root,
            kind="generated-source",
            project_root=self.project_root,
            required_subdirectories=(Path("sources") / "sha256", Path("staging")),
        )
        cas = FileSystemCAS(self.cas_root)
        assets = admit_locked_authored_assets(snapshot, cas=cas)
        planned = FilesystemStandardProjectPlanningAdapter(
            coding_cli_selector=lambda: self.generator.selection,
            model_selector=self.model_selector,
            native_sdk_inputs=self.native_sdk_inputs,
        ).plan(snapshot, assets=assets)
        node_adapter = LockedComponentNodePreparationAdapter(
            model_selector=self.model_selector,
            coding_cli=planned.coding_cli.name,
            native_sdk_inputs=self.native_sdk_inputs,
        )
        prepared = StandardProjectApplicationService.prepare(
            planned.execution_plan,
            authority=snapshot,
            authority_lock_identity=node_adapter.authority_lock_identity,
            authority_guard=node_adapter.guard,
            node_projector=node_adapter.project,
            workspace_allocator=FilesystemComponentWorkspaceAllocator(output).allocate,
            framework_envelope=lambda projection: projection.recipe.prompt(
                include_locked_authority_documents=False
            ).encode("utf-8"),
            budget=self.budget,
        )
        runner, cached = self.runner(
            snapshot, prepared.execution_plan, assets=assets, cas=cas
        )
        context_recorder = FilesystemForwardGenerationContextEvidenceRecorder(
            FilesystemForwardGenerationContextJournal(
                self.cas_root / "forward-generation-context"
            ),
            cache_key=lambda node: runner.planned_cache_key(node).identity,
        )

        def run_node(node: object):
            try:
                return runner(node)
            except Exception as error:
                raise ComponentSourceGenerationRunError(
                    getattr(error, "code", "runner-failed")
                ) from error

        revisions = tuple(
            plan.component_revision for plan in planned.execution_plan.generation_plans
        )
        invalidation = ComponentInvalidationDecision(
            "litai-generate",
            planned.execution_plan.root_revision,
            ComponentChangeSurface.LOCAL_AUTHORITY,
            revisions,
            revisions,
            revisions,
        )
        custody = StandardProjectApplicationService.generate_sources(
            prepared,
            invalidation=invalidation,
            runner=run_node,
            max_parallelism=max_parallelism,
            context_evidence_recorder=context_recorder,
        )
        snapshot.require_unchanged()
        context_report = context_recorder.cache_report()
        context_benchmarks = context_recorder.benchmark_records
        journal_identities = context_recorder.prompt_journal_identities
        admission_candidates = []
        for component in custody.components:
            if component.output is None:
                continue
            node = prepared.nodes_by_revision[component.component_revision.uri]
            provenance = component.output.provenance
            handoff = (
                self.generator.handoff_for(
                    component.output.candidate.planned_coding_cli_request_identity
                )
                if isinstance(self.generator, InheritedSessionSourceGenerator)
                else None
            )
            admission_candidates.append(
                GeneratedStandardSourceAdmissionCandidate(
                    SourceGenerationResumeCandidate(
                        component.output,
                        component.output.identity,
                        component.result.complexity_budget_identity,
                        component.result.complexity_decision_identity,
                    ),
                    runner.planned_cache_key(node),
                    Path(component.workspace_locator),
                    node.plan.generation_key.flavor_selection_identity,
                    canonical_identity(
                        {
                            "schema": "literate-ai/generation-skill-closure@1",
                            "skill_identities": [
                                item.uri
                                for item in node.plan.generation_key.skill_identities
                            ],
                        }
                    ),
                    (
                        handoff.identity
                        if handoff is not None
                        else provenance.model_stage_output_identities[-1]
                    ),
                    handoff,
                )
            )
        return GeneratedStandardProject(
            planned.coding_cli,
            custody,
            cached.report(
                context_report=context_report,
                context_benchmarks=context_benchmarks,
                context_prompt_journal_identities=journal_identities,
            ),
            journal_identities,
            context_benchmarks,
            context_report,
            tuple(admission_candidates),
        )

    def runner(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        execution_plan: ComponentExecutionPlan,
        *,
        assets: tuple[AuthoredBinaryAsset, ...] = (),
        cas: FileSystemCAS | None = None,
    ) -> tuple[CachedCodingCliSourceGenerationRunner, CachedCodingCliSourceGenerator]:
        """Bind the guarded coding CLI as a reusable Standard source runner."""

        cached = CachedCodingCliSourceGenerator(
            self.generator,
            cache_root=self.cache_root,
            project_root=self.project_root,
        )
        return (
            CachedCodingCliSourceGenerationRunner(
                cached,
                cas=cas or FileSystemCAS(self.cas_root),
                invocation_provider=self._invocation_provider(
                    snapshot,
                    execution_plan,
                ),
                assets=assets,
            ),
            cached,
        )

    def _invocation_provider(
        self,
        snapshot: LockedGenerationAuthoritySnapshot,
        execution_plan: ComponentExecutionPlan,
    ) -> Callable[[object], CodingCliSourceGenerationInvocation]:
        planner = GenerationExecutionPlanningAdapter()

        def provide(node: object) -> CodingCliSourceGenerationInvocation:
            recipe = node.recipe
            execution = planner.plan_node(
                snapshot,
                node,
                selection=self.generator.selection,
                model=recipe.model_for(self.generator.selection.name),
            )
            prior = {
                "component_revision": node.plan.component_revision.uri,
                "source_generation_request_identity": node.request.request.identity.uri,
                "component_generation_plan_identity": node.plan.identity.uri,
                "context_manifest_identity": (
                    node.request.request.context_manifest_identity.uri
                ),
                "workspace_allocation_identity": node.workspace.allocation_identity.uri,
                "recipe_identity": recipe.identity,
                "generation_key_identity": node.plan.generation_key.identity.uri,
                "required_entrypoints": list(recipe.all_required_entrypoints),
                "direct_public_interface_identities": [
                    item.uri
                    for item in (
                        node.plan.generation_key.direct_public_interface_identities
                    )
                ],
            }
            if getattr(self, "project_authority_identity", None) is not None:
                prior["project_authority_identity"] = (
                    self.project_authority_identity.uri
                )
            request = {
                "stage_id": execution.model_stages[-1].stage_id,
                "prior_stage_outputs": {"plan": prior},
                "input_identity": canonical_identity(prior).to_dict(),
            }
            return CodingCliSourceGenerationInvocation.create(
                execution,
                request,
                application_root_revision_identity=(execution_plan.root_revision),
                readiness_identity=canonical_identity(
                    {
                        "standard-source-readiness": node.plan.component_revision.uri,
                        "execution_plan": execution_plan.identity.uri,
                    }
                ),
            )

        return provide


__all__ = [
    "ExecutedStandardProject",
    "FilesystemStandardProjectRuntime",
    "FilesystemStandardProjectPlanningAdapter",
    "FilesystemStandardSourceGenerationAdapter",
    "GeneratedStandardSourceAdmissionCandidate",
    "GeneratedStandardProject",
    "LocalObservedToolchainAuthority",
    "PlannedStandardProject",
    "ProjectedStandardToolchainClosure",
    "PreparedStandardSourceCacheRestorer",
    "PreparedStandardLifecycleCheckpointStore",
    "ReleasedStandardProject",
    "StandardAcceptedSourceContinuationError",
    "StandardProjectPublicationRequest",
    "StandardProjectRuntimeReadiness",
    "StandardProjectExecutionRequest",
    "StandardCommandProjectionError",
    "admit_locked_authored_assets",
    "assemble_filesystem_standard_project_runtime",
    "compose_filesystem_standard_source_cache",
    "compose_filesystem_standard_lifecycle_checkpoints",
    "project_locked_standard_toolchain_closure",
    "project_standard_toolchain_closure",
]
