"""Authorization-gated native Mix compilation and retained Hex dependency trees."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.adapters.dependencies.admission import reconcile_generated_dependencies
from literate_ai.adapters.dependencies.hex_archive import verify_hex_archive
from literate_ai.adapters.dependencies.mix_lock import MixLock
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    LibraryImportSurface,
    canonical_identity,
)
from literate_ai.contracts.elixir_libraries import elixir_namespace
from literate_ai.contracts.mix_projects import MixProjectIntent
from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
)

from ._process import run_bounded_process
from .hex import HexToolchain
from .python import (
    BuildError,
    _require_source_tree_unchanged,
    canonical_tree_digest,
    require_unsandboxed_host_build_authorization,
)

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_FILES = 8192
_MAX_TREE_BYTES = 256 * 1024 * 1024
_DEPENDENCY_PROBE = """data = JSON.decode!(File.read!(hd(System.argv())))
versions = data["versions"]
Enum.each(data["requirements"], fn req ->
  {:ok, parsed} = Version.parse_requirement(req["requirement"])
  if version = versions[req["name"]], do:
    unless Version.match?(version, parsed), do: raise("locked requirement mismatch")
end)
metadata = Enum.map(data["archives"], fn archive ->
  {:ok, info} = :mix_hex_tarball.unpack({:file, String.to_charlist(archive)}, :none)
  info.metadata
end)
IO.puts(JSON.encode!(metadata))
"""
_RUNTIME_PROBE = """data = JSON.decode!(File.read!(hd(System.argv())))
normalize = fn path ->
  expanded = Path.expand(path)
  if match?({:win32, _}, :os.type()), do: String.downcase(expanded), else: expanded
end
Enum.each(data["packages"], fn pkg ->
  app = String.to_atom(pkg["name"])
  :ok = Application.load(app)
  unless List.to_string(Application.spec(app, :vsn)) == pkg["version"],
    do: raise("retained application version mismatch")
end)
files = Enum.map(data["modules"], fn path ->
  {:ok, {module, _}} = :beam_lib.chunks(String.to_charlist(path), [])
  Code.ensure_loaded!(module)
  unless normalize.(List.to_string(:code.which(module))) == normalize.(path),
    do: raise("retained module origin mismatch")
  %{module: Atom.to_string(module), path: path}
end)
unless length(Enum.uniq_by(files, & &1.module)) == length(files),
  do: raise("duplicate retained module")
IO.puts(JSON.encode!(files))
"""
_INVENTORY_PROBE = """data = JSON.decode!(File.read!(hd(System.argv())))
files = Enum.map(data["modules"], fn path ->
  {:ok, {module, _}} = :beam_lib.chunks(String.to_charlist(path), [])
  unless :code.which(module) == :non_existing,
    do: raise("retained module shadows an installed runtime module")
  %{module: Atom.to_string(module), path: path}
end)
unless length(Enum.uniq_by(files, & &1.module)) == length(files),
  do: raise("duplicate retained module")
IO.puts(JSON.encode!(files))
"""
_PROVIDER_PROBE = """data = JSON.decode!(File.read!(hd(System.argv())))
normalize = fn path ->
  expanded = Path.expand(path)
  if match?({:win32, _}, :os.type()), do: String.downcase(expanded), else: expanded
end
seen = Enum.map(data, fn provider ->
  Enum.map(provider["surface"]["capabilities"], fn capability ->
    module = Enum.find_value(provider["modules"], fn path ->
      {:ok, {candidate, _}} = :beam_lib.chunks(String.to_charlist(path), [])
      if Atom.to_string(candidate) == "Elixir." <> capability["module"], do: candidate
    end) || raise("missing provider module")
    Code.ensure_loaded!(module)
    origin = normalize.(List.to_string(:code.which(module)))
    unless origin in Enum.map(provider["modules"], normalize),
      do: raise("provider module escaped exact retained payload")
    exports = module.__info__(:functions) ++ module.__info__(:macros)
    Enum.each(capability["symbols"], fn symbol ->
      unless Enum.any?(exports, fn {name, _} -> Atom.to_string(name) == symbol end),
        do: raise("missing provider export")
    end)
    capability["capability"]
  end)
end)
IO.puts(JSON.encode!(seen))
"""


def _read(path: Path) -> bytes:
    if (
        path_is_link_or_reparse(path)
        or not path.is_file()
        or path.stat().st_size > _MAX_FILE_BYTES
    ):
        raise BuildError(
            "builder.mix_payload_invalid", "Mix requires bounded regular files"
        )
    content = path.read_bytes()
    if len(content) > _MAX_FILE_BYTES:
        raise BuildError(
            "builder.mix_payload_invalid", "Mix payload exceeded its bound"
        )
    return content


def _beam_paths(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.glob("*.beam"), key=lambda path: path.name))


def _files(root: Path) -> dict[str, bytes]:
    if path_is_link_or_reparse(root) or not root.is_dir():
        raise BuildError("builder.mix_payload_invalid", "Mix requires a regular tree")
    result = {}
    total = 0
    for path in sorted(
        root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()
    ):
        if path_is_link_or_reparse(path):
            raise BuildError(
                "builder.mix_payload_invalid", "Mix rejects linked payloads"
            )
        if path.is_dir():
            continue
        content = _read(path)
        total += len(content)
        if len(result) >= _MAX_FILES or total > _MAX_TREE_BYTES:
            raise BuildError(
                "builder.mix_payload_invalid", "Mix tree exceeded its budget"
            )
        result[path.relative_to(root).as_posix()] = content
    return result


def _require_metadata(metadata: object, lock: MixLock) -> None:
    if not isinstance(metadata, list) or len(metadata) != len(lock.packages):
        raise BuildError(
            "builder.mix_metadata_mismatch", "Hex metadata inventory differs"
        )
    for info, package in zip(metadata, lock.packages, strict=True):
        if not isinstance(info, dict) or (
            info.get("name"),
            info.get("app"),
            info.get("version"),
            info.get("build_tools"),
        ) != (package.name, package.name, package.version, ["mix"]):
            raise BuildError(
                "builder.mix_metadata_mismatch", "Hex metadata names another package"
            )
        requirements = info.get("requirements")
        expected = {item.name: item for item in package.dependencies}
        if not isinstance(requirements, dict) or set(requirements) != set(expected):
            raise BuildError(
                "builder.mix_metadata_mismatch", "Hex metadata graph differs"
            )
        for name, value in requirements.items():
            dependency = expected[name]
            if (
                not isinstance(value, dict)
                or not set(value) <= {"app", "requirement", "optional", "repository"}
                or value.get("app", name) != name
                or value.get("repository", "hexpm") != "hexpm"
                or type(value.get("optional", False)) is not bool
                or value.get("optional", False) != dependency.optional
                or value.get("requirement") != dependency.requirement
            ):
                raise BuildError(
                    "builder.mix_metadata_mismatch", "Hex metadata edge differs"
                )


@dataclass(frozen=True)
class MixProviderApplication:
    """One independently pinned retained application in a provider closure."""

    name: str
    version: str
    ebin: Path
    tree_digest: str

    def __post_init__(self):
        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name) is None
            or not isinstance(self.version, str)
            or re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?", self.version
            )
            is None
            or not isinstance(self.ebin, Path)
            or not self.ebin.is_absolute()
            or ".." in self.ebin.parts
        ):
            raise TypeError("Provider application requires exact native authority")
        ContentIdentity.parse_uri(self.tree_digest)

    def require_unchanged(self):
        try:
            require_safe_directory(self.ebin)
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise BuildError(
                "builder.mix_provider_invalid", "Unsafe provider application"
            ) from exc
        files = _files(self.ebin)
        if f"{self.name}.app" not in files or any(
            "/" in name or (not name.endswith(".beam") and name != f"{self.name}.app")
            for name in files
        ):
            raise BuildError(
                "builder.mix_provider_invalid", "Provider application payload differs"
            )
        if canonical_tree_digest(self.ebin) != self.tree_digest:
            raise BuildError(
                "builder.mix_provider_changed", "Provider application changed"
            )

    def to_dict(self):
        return {
            "name": self.name,
            "version": self.version,
            "tree_digest": self.tree_digest,
        }


@dataclass(frozen=True)
class MixProviderLibrary:
    """Explicit compiled input supplied by the trusted lifecycle owner.

    These pins describe payload custody, never grant build permission. The owner
    still checks published artifact and dependency-edge authority at each phase.
    """

    artifact_identity: ContentIdentity
    import_surface: LibraryImportSurface
    ebin: Path
    tree_digest: str
    applications: tuple[MixProviderApplication, ...] = ()

    def __post_init__(self):
        if (
            not isinstance(self.artifact_identity, ContentIdentity)
            or not isinstance(self.import_surface, LibraryImportSurface)
            or self.import_surface.language != "elixir"
            or not isinstance(self.ebin, Path)
            or not self.ebin.is_absolute()
            or ".." in self.ebin.parts
        ):
            raise TypeError("Mix provider requires exact Elixir library authority")
        ContentIdentity.parse_uri(self.tree_digest)
        if not isinstance(self.applications, tuple) or any(
            not isinstance(item, MixProviderApplication) for item in self.applications
        ):
            raise TypeError("Provider application closure requires typed selections")
        if self.applications and (
            len({item.name for item in self.applications}) != len(self.applications)
            or self.applications[0].name != self.import_surface.package
            or self.applications[0].ebin != self.ebin
            or self.applications[0].tree_digest != self.tree_digest
        ):
            raise ValueError(
                "Provider application closure must start with its exact library"
            )

    @property
    def code_paths(self):
        return (
            tuple(item.ebin for item in self.applications)
            if self.applications
            else (self.ebin,)
        )

    def require_unchanged(self):
        try:
            require_safe_directory(self.ebin)
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise BuildError(
                "builder.mix_provider_invalid", "Provider directory is unsafe"
            ) from exc
        files = _files(self.ebin)
        allowed_app = (
            f"{self.import_surface.package}.app" if self.applications else None
        )
        if not files or any(
            "/" in name or (Path(name).suffix != ".beam" and name != allowed_app)
            for name in files
        ):
            raise BuildError(
                "builder.mix_provider_invalid",
                "Provider requires a flat compiled BEAM library closure",
            )
        if canonical_tree_digest(self.ebin) != self.tree_digest:
            raise BuildError(
                "builder.mix_provider_changed", "Selected provider payload changed"
            )
        for application in self.applications:
            application.require_unchanged()

    def to_dict(self):
        record = {
            "artifact_identity": self.artifact_identity.uri,
            "import_surface": self.import_surface.to_dict(),
            "tree_digest": self.tree_digest,
        }
        if self.applications:
            record["applications"] = [item.to_dict() for item in self.applications]
        return record


@dataclass(frozen=True)
class MixBuildArtifact:
    artifact_digest: str
    artifact_path: Path
    source_bundle_digest: str
    authorization_id: str
    toolchain_identity: str
    runtime_command: tuple[str, ...]
    dependency_evidence_identity: str


def _provider_code_paths(providers):
    """Expose one exact payload per application; refuse conflicting closures."""
    paths = []
    applications = {}
    for provider in providers:
        if not provider.applications:
            paths.append(provider.ebin)
        for application in provider.applications:
            previous = applications.get(application.name)
            pin = (application.version, application.tree_digest)
            if previous is not None:
                if previous != pin:
                    raise BuildError(
                        "builder.mix_provider_conflict",
                        "Provider application closures conflict",
                    )
                continue
            applications[application.name] = pin
            paths.append(application.ebin)
    return tuple(paths)


def _provider_directory(root, identity, index, *, layout="ordinal-v1"):
    if layout == "ordinal-v1":
        return root / "providers" / str(index)
    if layout == "digest-v1":
        return root / "providers" / identity.digest
    raise ValueError("Unknown retained Mix provider layout")


def verify_mix_artifact(
    artifact: MixBuildArtifact,
    *,
    toolchain: HexToolchain,
    history_verifier: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    """Revalidate against independently retained build identities, never self-pin."""
    if (
        not isinstance(artifact, MixBuildArtifact)
        or artifact.toolchain_identity != toolchain.identity
        or canonical_tree_digest(artifact.artifact_path) != artifact.artifact_digest
    ):
        raise BuildError("builder.mix_artifact_changed", "Retained Mix tree differs")
    root = artifact.artifact_path
    files = _files(root)
    manifest_path = ".literate/mix/evidence-manifest.json"
    try:
        document = json.loads(files.pop(manifest_path))
        if (
            canonical_identity(document).uri != artifact.dependency_evidence_identity
            or document["source_bundle_digest"] != artifact.source_bundle_digest
            or document["authorization_id"] != artifact.authorization_id
            or document["toolchain_identity"] != toolchain.identity
            or document["files"]
            != [
                {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in files.items()
            ]
        ):
            raise ValueError("Mix evidence differs from independent build identities")
        request = BuildRequest.from_dict(document["request"])
        authorization = BuildAuthorization.from_dict(document["authorization"])
        authorization.require_valid(request, now=authorization.issued_at)
        if (
            canonical_identity(request.to_dict()).uri != document["request_identity"]
            or request.toolchain_digest != toolchain.identity
            or request.source_bundle_digest != artifact.source_bundle_digest
            or authorization.authorization_id != artifact.authorization_id
        ):
            raise ValueError("Mix historical build authority differs")
        if "standard_authority" in document:
            if history_verifier is None:
                raise ValueError("Standard Mix history requires its lifecycle owner")
            history_verifier(document)
        else:
            require_unsandboxed_host_build_authorization(request, authorization)
            if (
                request.builder_id
                != GuardedMixBuilder.provider_bound_builder_id(
                    document.get("provider_libraries", []),
                    library_import_surface=document.get("library_import_surface"),
                )
                or "elixir-mix-tree" not in request.allowed_outputs
            ):
                raise ValueError("Mix historical builder or output differs")
        project = MixProjectIntent.from_bytes(files["source/mix-project.json"])
        if project.to_dict() != document["project"]:
            raise ValueError("Mix project differs from retained source")
        if "library_import_surface" in document:
            surface = LibraryImportSurface.from_dict(document["library_import_surface"])
            if surface.language != "elixir" or surface.package != project.app:
                raise ValueError("Retained Mix library authority differs")
        lock = MixLock.from_bytes(files[".literate/mix/mix.lock"], project=project)
        if [list(edge) for edge in lock.edges] != document["lock_edges"]:
            raise ValueError("Mix graph differs from retained native lock")
        acquired = [
            verify_hex_archive(
                files[f".literate/mix/{package.name}-{package.version}.tar"],
                package=package,
            ).to_dict()
            for package in lock.packages
        ]
        if acquired != document["acquired_archives"]:
            raise ValueError("Mix acquired bytes differ from retained evidence")
        _require_metadata(document["native_metadata"], lock)
        provider_records = document.get("provider_libraries", [])
        provider_layout = document.get("provider_layout", "digest-v1")
        if provider_layout not in ("ordinal-v1", "digest-v1"):
            raise ValueError("Unknown retained Mix provider layout")
        if not isinstance(provider_records, list):
            raise ValueError("Invalid provider inventory")
        identities = set()
        for provider_index, record in enumerate(provider_records):
            if not isinstance(record, dict) or set(record) not in (
                {
                    "artifact_identity",
                    "import_surface",
                    "tree_digest",
                },
                {"artifact_identity", "import_surface", "tree_digest", "applications"},
            ):
                raise ValueError("Invalid provider record")
            identity = ContentIdentity.parse_uri(record["artifact_identity"])
            if identity in identities:
                raise ValueError("Provider lacks unique artifact authority")
            identities.add(identity)
            provider_root = _provider_directory(
                root,
                identity,
                provider_index,
                layout=provider_layout,
            )
            application_records = record.get("applications", [])
            if not isinstance(application_records, list) or any(
                not isinstance(item, dict)
                or set(item) != {"name", "version", "tree_digest"}
                for item in application_records
            ):
                raise ValueError("Invalid provider application records")
            applications = tuple(
                MixProviderApplication(
                    item["name"],
                    item["version"],
                    (
                        provider_root / "ebin"
                        if index == 0
                        else provider_root / "applications" / item["name"] / "ebin"
                    ).resolve(),
                    item["tree_digest"],
                )
                for index, item in enumerate(application_records)
            )
            provider = MixProviderLibrary(
                identity,
                LibraryImportSurface.from_dict(record["import_surface"]),
                (provider_root / "ebin").resolve(),
                record["tree_digest"],
                applications,
            )
            provider.require_unchanged()
    except (ValueError, TypeError, KeyError) as exc:
        raise BuildError(
            "builder.mix_artifact_changed", "Retained Mix evidence differs"
        ) from exc
    if canonical_tree_digest(root) != artifact.artifact_digest:
        raise BuildError(
            "builder.mix_artifact_changed", "Mix tree changed during verification"
        )
    return document


class GuardedMixBuilder:
    """Derive/build only in an authorized external projection; retain all bytes."""

    builder_id = "builder:elixir-mix@1"

    @classmethod
    def provider_bound_builder_id(cls, records, *, library_import_surface=None):
        """Bind the acknowledged request to every compiled provider input."""
        if not isinstance(records, list):
            raise TypeError("Provider builder binding requires a record list")
        if library_import_surface is not None:
            surface = LibraryImportSurface.from_dict(library_import_surface)
            if surface.language != "elixir":
                raise ValueError("Mix library authority requires Elixir")
        if not records and library_import_surface is None:
            return cls.builder_id
        return canonical_identity(
            {
                "builder_id": cls.builder_id,
                "provider_libraries": records,
                **(
                    {"library_import_surface": library_import_surface}
                    if library_import_surface is not None
                    else {}
                ),
            }
        ).uri

    def __init__(
        self,
        toolchain: HexToolchain,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: float = 900,
    ):
        if not isinstance(toolchain, HexToolchain):
            raise TypeError("Mix build requires an observed loadable Hex toolchain")
        if timeout_seconds <= 0:
            raise ValueError("Mix process timeout must be positive")
        self.toolchain = toolchain
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.clock = clock
        self.timeout_seconds = timeout_seconds

    def build(
        self,
        request: BuildRequest,
        authorization: BuildAuthorization,
        *,
        source_root: Path,
        artifact_store: Path,
        now: datetime,
        provider_libraries: tuple[MixProviderLibrary, ...] = (),
        library_import_surface: LibraryImportSurface | None = None,
    ) -> MixBuildArtifact:
        if library_import_surface is not None and not isinstance(
            library_import_surface, LibraryImportSurface
        ):
            raise TypeError("Mix library production requires a typed import surface")
        if not isinstance(provider_libraries, tuple) or any(
            not isinstance(item, MixProviderLibrary) for item in provider_libraries
        ):
            raise TypeError("Mix providers require immutable typed selections")
        self.authorization_verifier.require_build_valid(authorization, request, now=now)
        require_unsandboxed_host_build_authorization(request, authorization)
        if (
            request.builder_id
            != self.provider_bound_builder_id(
                [item.to_dict() for item in provider_libraries],
                library_import_surface=library_import_surface.to_dict()
                if library_import_surface
                else None,
            )
            or request.toolchain_digest != self.toolchain.identity
        ):
            raise BuildError(
                "builder.mix_authority_mismatch",
                "Mix build selected another builder or toolchain",
            )
        if "elixir-mix-tree" not in request.allowed_outputs:
            raise BuildError(
                "builder.output_not_authorized", "Mix tree output is not authorized"
            )

        def require_authority():
            self.authorization_verifier.require_build_valid(
                authorization, request, now=self.clock()
            )
            require_unsandboxed_host_build_authorization(request, authorization)

        return self._build_authorized(
            request,
            authorization,
            source_root=source_root,
            artifact_store=artifact_store,
            source_tree_digest=request.source_bundle_digest,
            require_authority=require_authority,
            provider_libraries=provider_libraries,
            library_import_surface=library_import_surface,
        )

    def _build_authorized(
        self,
        request: BuildRequest,
        authorization: BuildAuthorization,
        *,
        source_root: Path,
        artifact_store: Path,
        source_tree_digest: str,
        require_authority: Callable[[], None],
        standard_authority: Mapping[str, object] | None = None,
        provider_libraries: tuple[MixProviderLibrary, ...] = (),
        library_import_surface: LibraryImportSurface | None = None,
    ) -> MixBuildArtifact:
        """Native producer shared with a trusted Standard lifecycle authority.

        The owner supplies exact live plan/request/grant checks. Source tree bytes
        have a separate canonical digest: never equate Standard's bundle identity
        with that digest. This private hook is not serialized build admission.
        """
        require_authority()
        if not isinstance(provider_libraries, tuple) or any(
            not isinstance(item, MixProviderLibrary) for item in provider_libraries
        ):
            raise TypeError("Mix providers require immutable typed selections")
        if (
            provider_libraries
            and standard_authority is None
            and (
                request.builder_id
                != self.provider_bound_builder_id(
                    [item.to_dict() for item in provider_libraries],
                    library_import_surface=library_import_surface.to_dict()
                    if library_import_surface
                    else None,
                )
            )
        ):
            raise BuildError(
                "builder.mix_provider_authority",
                "Provider inputs differ from their acknowledged request",
            )
        if len({item.artifact_identity for item in provider_libraries}) != len(
            provider_libraries
        ):
            raise BuildError("builder.mix_provider_invalid", "Duplicate provider input")
        for item in provider_libraries:
            item.require_unchanged()
        source = source_root.resolve(strict=True)
        _require_source_tree_unchanged(source, source_tree_digest)
        source_files = _files(source)
        if any(
            Path(name).name in {"mix.exs", "mix.lock", ".iex.exs"}
            for name in source_files
        ):
            raise BuildError(
                "builder.mix_source_authority",
                "Generated Mix source must not supply executable project "
                "or lock authority",
            )
        if set(
            name for name in source_files if Path(name).name == "mix-project.json"
        ) != {"source/mix-project.json"}:
            raise BuildError(
                "builder.mix_source_authority",
                "Mix requires one canonical declarative project root",
            )
        project = MixProjectIntent.from_bytes(source_files["source/mix-project.json"])
        if library_import_surface is not None:
            if (
                not isinstance(library_import_surface, LibraryImportSurface)
                or library_import_surface.language != "elixir"
                or project.app != library_import_surface.package
            ):
                raise BuildError(
                    "builder.mix_library_authority",
                    "Mix library differs from declared package authority",
                )
            prefix = f"source/{library_import_surface.package}/"
            modules = [name for name in source_files if name.endswith(".ex")]
            if not modules or any(not name.startswith(prefix) for name in modules):
                raise BuildError(
                    "builder.mix_library_authority",
                    "Mix library sources escape the declared package",
                )
        bom = source_files.get(CYCLONEDX_SOURCE_SBOM_PATH)
        if bom is None:
            raise BuildError(
                "builder.mix_source_bom_missing",
                "Mix requires admitted source BOM bytes",
            )
        text_files = {}
        for name, content in source_files.items():
            try:
                text_files[name] = content.decode("utf-8")
            except UnicodeError:
                continue
        reconcile_generated_dependencies(text_files, bom)
        if library_import_surface is None and "source/main.exs" not in source_files:
            raise BuildError(
                "builder.mix_entrypoint_missing", "Mix requires source/main.exs"
            )
        if artifact_store.resolve().is_relative_to(source):
            raise BuildError(
                "builder.mix_projection_invalid", "Mix objects must be outside source"
            )
        artifact_store.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix="mix-build-", dir=artifact_store)
        ).resolve()
        projection = staging / "projection"
        objects = staging / "objects"
        retained = staging / "retained"
        phases = []
        try:
            shutil.copytree(source, projection)
            objects.mkdir()
            retained.mkdir()
            evidence = retained / ".literate/mix"
            (evidence / "process").mkdir(parents=True)
            provider_paths = []
            provider_modules = []
            provider_observations = []
            retained_providers = []
            for provider_index, item in enumerate(provider_libraries):
                destination = (
                    _provider_directory(
                        retained, item.artifact_identity, provider_index
                    )
                    / "ebin"
                )
                shutil.copytree(item.ebin, destination)
                if canonical_tree_digest(destination) != item.tree_digest:
                    raise BuildError(
                        "builder.mix_provider_changed", "Provider changed during copy"
                    )
                copied_applications = []
                for index, application in enumerate(item.applications):
                    copied = (
                        destination
                        if index == 0
                        else destination.parent
                        / "applications"
                        / application.name
                        / "ebin"
                    )
                    if index:
                        shutil.copytree(application.ebin, copied)
                    copied_applications.append(replace(application, ebin=copied))
                retained_provider = replace(
                    item, ebin=destination, applications=tuple(copied_applications)
                )
                retained_provider.require_unchanged()
                retained_providers.append(retained_provider)
                modules = [str(path) for path in _beam_paths(destination)]
                provider_observations.append(
                    {"surface": item.import_surface.to_dict(), "modules": modules}
                )
            provider_paths = list(_provider_code_paths(retained_providers))
            provider_modules = [
                str(path) for ebin in provider_paths for path in _beam_paths(ebin)
            ]
            package_files = tuple(
                sorted(
                    {
                        "mix.exs",
                        "mix.lock",
                        *(
                            name.removeprefix("source/")
                            for name in source_files
                            if name.startswith("source/")
                        ),
                    }
                )
            )
            native_project = project.native_project(
                package_files=package_files,
                provider_ebins=tuple(map(str, provider_paths)),
            )
            (projection / "source/mix.exs").write_bytes(native_project)
            archives = objects / "archives/hex"
            copied_ebin = archives / "hex/ebin"
            copied_ebin.mkdir(parents=True)
            for path, _ in self.toolchain.file_bindings:
                shutil.copy2(path, copied_ebin / Path(path).name)
            expected_hex = {
                Path(path).name: digest for path, digest in self.toolchain.file_bindings
            }
            environment = {
                name: value
                for name, value in os.environ.items()
                if not name.upper().startswith(("MIX_", "HEX_"))
                and name.upper()
                not in {"ERL_LIBS", "ERL_FLAGS", "ERL_AFLAGS", "ELIXIR_ERL_OPTIONS"}
            }
            environment.update(
                {
                    "MIX_HOME": str(objects / "mix-home"),
                    "HEX_HOME": str(objects / "hex-home"),
                    "MIX_ARCHIVES": str(objects / "archives"),
                    "MIX_BUILD_PATH": str(objects / "build"),
                    "MIX_DEPS_PATH": str(objects / "deps"),
                    "MIX_ENV": "prod",
                    "MIX_EXS": str(projection / "source/mix.exs"),
                }
            )
            command = (
                *self.toolchain.mix.elixir.command,
                *(token for path in provider_paths for token in ("-pa", str(path))),
                "-pa",
                str(copied_ebin),
                self.toolchain.mix.file_bindings[0][0],
            )
            derived_lock = None

            def require_projection():
                current = _files(projection)
                if current.pop("source/mix.exs", None) != native_project:
                    raise BuildError(
                        "builder.mix_projection_changed", "Native Mix project changed"
                    )
                projected_lock = current.pop("source/mix.lock", None)
                if current != source_files or (
                    derived_lock is not None and projected_lock != derived_lock
                ):
                    raise BuildError(
                        "builder.mix_projection_changed", "Mix input projection changed"
                    )

            def run(argv, *, cwd, phase):
                require_authority()
                for item, copied in zip(
                    provider_libraries, retained_providers, strict=True
                ):
                    item.require_unchanged()
                    copied.require_unchanged()
                self.toolchain.require_unchanged(environment)
                _require_source_tree_unchanged(source, source_tree_digest)
                require_projection()
                before = canonical_tree_digest(copied_ebin)
                if {
                    name: "sha256:" + hashlib.sha256(data).hexdigest()
                    for name, data in _files(copied_ebin).items()
                } != expected_hex:
                    raise BuildError(
                        "builder.hex_toolchain_changed",
                        "Staged Hex differs from its pin",
                    )
                try:
                    result = run_bounded_process(
                        argv,
                        cwd=cwd,
                        environment=environment,
                        timeout_seconds=self.timeout_seconds,
                        stdout_limit_bytes=4 * 1024 * 1024,
                        stderr_limit_bytes=4 * 1024 * 1024,
                        error_prefix="builder.mix",
                    )
                finally:
                    require_authority()
                    for item, copied in zip(
                        provider_libraries, retained_providers, strict=True
                    ):
                        item.require_unchanged()
                        copied.require_unchanged()
                    self.toolchain.require_unchanged(environment)
                    _require_source_tree_unchanged(source, source_tree_digest)
                    require_projection()
                    if canonical_tree_digest(copied_ebin) != before:
                        raise BuildError(
                            "builder.hex_toolchain_changed",
                            "Staged Hex payload changed during build",
                        )
                if result.returncode:
                    detail = result.stderr.decode("utf-8", errors="replace")
                    raise BuildError(
                        "builder.mix_failed", f"Mix {phase} failed: {detail}"
                    )
                record = {
                    "phase": phase,
                    "returncode": result.returncode,
                    "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                }
                (evidence / "process" / f"{phase}.stdout").write_bytes(result.stdout)
                (evidence / "process" / f"{phase}.stderr").write_bytes(result.stderr)
                phases.append(record)
                return result

            if provider_libraries:
                provider_probe = objects / "provider-inventory.json"
                provider_probe.write_text(
                    json.dumps({"modules": provider_modules}), encoding="utf-8"
                )
                inventory = run(
                    (
                        *self.toolchain.mix.elixir.command,
                        "-e",
                        _INVENTORY_PROBE,
                        "--",
                        str(provider_probe),
                    ),
                    cwd=objects,
                    phase="provider-module-inventory",
                )
                observed = json.loads(inventory.stdout)
                if (
                    not isinstance(observed, list)
                    or [item.get("path") for item in observed if isinstance(item, dict)]
                    != provider_modules
                ):
                    raise BuildError(
                        "builder.mix_provider_invalid",
                        "Provider module inventory differs",
                    )
                for provider, observation in zip(
                    provider_libraries, provider_observations, strict=True
                ):
                    namespace = "Elixir." + elixir_namespace(
                        provider.import_surface.package
                    )
                    for module in observed:
                        if module["path"] in observation["modules"] and not (
                            module["module"] == namespace
                            or module["module"].startswith(namespace + ".")
                        ):
                            raise BuildError(
                                "builder.mix_provider_invalid",
                                "Provider module escapes its package namespace",
                            )
                if any(item.applications for item in retained_providers):
                    apps = {
                        application.name: application.version
                        for item in retained_providers
                        for application in item.applications
                    }
                    provider_probe.write_text(
                        json.dumps(
                            {
                                "modules": provider_modules,
                                "packages": [
                                    {"name": name, "version": version}
                                    for name, version in sorted(apps.items())
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    checked = run(
                        (
                            *self.toolchain.mix.elixir.command,
                            *(
                                token
                                for path in provider_paths
                                for token in ("-pa", str(path))
                            ),
                            "-e",
                            _RUNTIME_PROBE,
                            "--",
                            str(provider_probe),
                        ),
                        cwd=objects,
                        phase="provider-application-verification",
                    )
                    if json.loads(checked.stdout) != observed:
                        raise BuildError(
                            "builder.mix_provider_invalid",
                            "Provider application module origins differ",
                        )
            run((*command, "deps.get"), cwd=projection / "source", phase="deps-get")
            lock_path = projection / "source/mix.lock"
            lock_bytes = _read(lock_path) if project.dependencies else b"%{}\n"
            if not project.dependencies:
                lock_path.write_bytes(lock_bytes)
            derived_lock = lock_bytes
            lock = MixLock.from_bytes(lock_bytes, project=project)
            reconcile_generated_dependencies(
                {**text_files, "source/mix.lock": lock_bytes.decode()}, bom
            )
            (evidence / "mix.lock").write_bytes(lock_bytes)
            (evidence / "mix.exs").write_bytes(native_project)
            acquired = []
            archive_paths = []
            for package in lock.packages:
                archive_name = f"{package.name}-{package.version}.tar"
                content = _read(objects / "hex-home/packages/hexpm" / archive_name)
                acquired.append(verify_hex_archive(content, package=package).to_dict())
                archive_path = evidence / archive_name
                archive_path.write_bytes(content)
                archive_paths.append(str(archive_path))
            requirements = [item.to_dict() for item in project.dependencies]
            requirements.extend(
                {"name": item.name, "requirement": item.requirement}
                for pkg in lock.packages
                for item in pkg.dependencies
            )
            probe_path = objects / "dependency-probe.json"
            probe_path.write_text(
                json.dumps(
                    {
                        "versions": {p.name: p.version for p in lock.packages},
                        "requirements": requirements,
                        "archives": archive_paths,
                    }
                ),
                encoding="utf-8",
            )
            native = run(
                (
                    *self.toolchain.mix.elixir.command,
                    "-pa",
                    str(copied_ebin),
                    "-e",
                    _DEPENDENCY_PROBE,
                    "--",
                    str(probe_path),
                ),
                cwd=objects,
                phase="dependency-verification",
            )
            metadata = json.loads(native.stdout)
            _require_metadata(metadata, lock)
            run(
                (*command, "compile", "--warnings-as-errors"),
                cwd=projection / "source",
                phase="compile",
            )
            if project.dependencies and _read(lock_path) != lock_bytes:
                raise BuildError(
                    "builder.mix_lock_changed", "Mix compile changed the verified lock"
                )
            run(
                (*command, "hex.build", "--output", str(evidence / "package.tar")),
                cwd=projection / "source",
                phase="package",
            )
            if project.dependencies and _read(lock_path) != lock_bytes:
                raise BuildError(
                    "builder.mix_lock_changed",
                    "Hex packaging changed the verified lock",
                )
            shutil.copytree(source / "source", retained / "source")
            runtime_root = retained / "runtime"
            runtime_root.mkdir()
            packages = [
                {"name": project.app, "version": project.version},
                *({"name": p.name, "version": p.version} for p in lock.packages),
            ]
            if {path.name for path in (objects / "build/lib").iterdir()} != {
                p["name"] for p in packages
            }:
                raise BuildError(
                    "builder.mix_runtime_mismatch",
                    "Native build contains an unexpected application",
                )
            code_paths = list(provider_paths)
            modules = list(provider_modules)
            runtime_applications = {
                app.name: (app.version, app.tree_digest)
                for item in retained_providers
                for app in item.applications
            }
            provider_packages = [
                {"name": name, "version": pin[0]}
                for name, pin in sorted(runtime_applications.items())
            ]
            for pkg in packages:
                ebin = objects / "build/lib" / pkg["name"] / "ebin"
                payload = _files(ebin)
                if f"{pkg['name']}.app" not in payload or any(
                    Path(name).suffix not in {".beam", ".app"} or "/" in name
                    for name in payload
                ):
                    raise BuildError(
                        "builder.mix_runtime_mismatch",
                        "Native application lacks a regular BEAM payload",
                    )
                destination = runtime_root / pkg["name"] / "ebin"
                shutil.copytree(ebin, destination)
                if pkg["name"] in runtime_applications:
                    if runtime_applications[pkg["name"]] != (
                        pkg["version"],
                        canonical_tree_digest(destination),
                    ):
                        raise BuildError(
                            "builder.mix_provider_conflict",
                            "Consumer and provider applications conflict",
                        )
                    continue
                code_paths.append(destination)
                modules.extend(
                    str(destination / name)
                    for name in payload
                    if name.endswith(".beam")
                )
            probe_path = objects / "runtime-probe.json"
            probe_path.write_text(
                json.dumps(
                    {
                        "packages": [
                            *provider_packages,
                            *(
                                pkg
                                for pkg in packages
                                if pkg["name"] not in runtime_applications
                            ),
                        ],
                        "modules": modules,
                    }
                ),
                encoding="utf-8",
            )
            inventory = run(
                (
                    *self.toolchain.mix.elixir.command,
                    "-e",
                    _INVENTORY_PROBE,
                    "--",
                    str(probe_path),
                ),
                cwd=objects,
                phase="retained-module-inventory",
            )
            module_inventory = json.loads(inventory.stdout)
            if (
                not isinstance(module_inventory, list)
                or [
                    item.get("path")
                    for item in module_inventory
                    if isinstance(item, dict)
                ]
                != modules
                or len(module_inventory) != len(modules)
            ):
                raise BuildError(
                    "builder.mix_runtime_mismatch", "Module inventory differs"
                )
            runtime_argv = (
                *self.toolchain.mix.elixir.command,
                *(token for path in code_paths for token in ("-pa", str(path))),
            )
            payload_before = {
                name: content
                for name, content in _files(retained).items()
                if not name.startswith(".literate/mix/process/")
            }
            observed = run(
                (*runtime_argv, "-e", _RUNTIME_PROBE, "--", str(probe_path)),
                cwd=retained,
                phase="retained-runtime-verification",
            )
            if {
                name: content
                for name, content in _files(retained).items()
                if not name.startswith(".literate/mix/process/")
            } != payload_before:
                raise BuildError(
                    "builder.mix_runtime_changed",
                    "Retained runtime changed during native observation",
                )
            observed_modules = json.loads(observed.stdout)
            if observed_modules != module_inventory:
                raise BuildError(
                    "builder.mix_runtime_mismatch",
                    "Loaded modules differ from inventory",
                )
            if library_import_surface is not None:
                library_modules = [
                    str(path)
                    for path in _beam_paths(runtime_root / project.app / "ebin")
                ]
                namespace = "Elixir." + elixir_namespace(library_import_surface.package)
                if not library_modules or any(
                    item["path"] in library_modules
                    and not (
                        item["module"] == namespace
                        or item["module"].startswith(namespace + ".")
                    )
                    for item in observed_modules
                ):
                    raise BuildError(
                        "builder.mix_library_namespace",
                        "Produced library escapes its declared namespace",
                    )
                probe_path.write_text(
                    json.dumps(
                        [
                            {
                                "surface": library_import_surface.to_dict(),
                                "modules": library_modules,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                before_library_probe = _files(retained)
                result = run(
                    (*runtime_argv, "-e", _PROVIDER_PROBE, "--", str(probe_path)),
                    cwd=retained,
                    phase="library-interface-verification",
                )
                if {
                    name: content
                    for name, content in _files(retained).items()
                    if not name.startswith(".literate/mix/process/")
                } != {
                    name: content
                    for name, content in before_library_probe.items()
                    if not name.startswith(".literate/mix/process/")
                }:
                    raise BuildError(
                        "builder.mix_runtime_changed",
                        "Library payload changed during interface observation",
                    )
                if json.loads(result.stdout) != [
                    [cap.capability for cap in library_import_surface.capabilities]
                ]:
                    raise BuildError(
                        "builder.mix_library_interface",
                        "Produced library public interface differs",
                    )
            if provider_libraries:
                probe_path.write_text(
                    json.dumps(provider_observations), encoding="utf-8"
                )
                payload_before = {
                    name: content
                    for name, content in _files(retained).items()
                    if not name.startswith(".literate/mix/process/")
                }
                provider_result = run(
                    (*runtime_argv, "-e", _PROVIDER_PROBE, "--", str(probe_path)),
                    cwd=retained,
                    phase="provider-interface-verification",
                )
                if {
                    name: content
                    for name, content in _files(retained).items()
                    if not name.startswith(".literate/mix/process/")
                } != payload_before:
                    raise BuildError(
                        "builder.mix_runtime_changed",
                        "Retained runtime changed during provider observation",
                    )
                expected = [
                    [
                        capability.capability
                        for capability in item.import_surface.capabilities
                    ]
                    for item in provider_libraries
                ]
                if json.loads(provider_result.stdout) != expected:
                    raise BuildError(
                        "builder.mix_provider_invalid", "Provider interface differs"
                    )
            for item in observed_modules:
                item["path"] = Path(item["path"]).relative_to(retained).as_posix()
            for package in lock.packages:
                verify_hex_archive(
                    _read(evidence / f"{package.name}-{package.version}.tar"),
                    package=package,
                )
            for phase in phases:
                for stream in ("stdout", "stderr"):
                    observed_digest = hashlib.sha256(
                        _read(evidence / "process" / f"{phase['phase']}.{stream}")
                    ).hexdigest()
                    if observed_digest != phase[f"{stream}_sha256"]:
                        raise BuildError(
                            "builder.mix_evidence_changed",
                            "Native process evidence changed",
                        )
            document = {
                "schema": "literate-ai/mix-build-evidence@1",
                "builder_id": self.builder_id,
                "request": request.to_dict(),
                "authorization": authorization.to_dict(),
                "source_bundle_digest": request.source_bundle_digest,
                "request_identity": canonical_identity(request.to_dict()).uri,
                "authorization_id": authorization.authorization_id,
                "toolchain_identity": self.toolchain.identity,
                "project": project.to_dict(),
                "lock_edges": lock.edges,
                "acquired_archives": acquired,
                "native_metadata": metadata,
                "runtime_modules": observed_modules,
                "provider_libraries": [item.to_dict() for item in provider_libraries],
                "provider_layout": "ordinal-v1",
                "phases": phases,
                "files": [
                    {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                    for name, content in _files(retained).items()
                ],
            }
            if standard_authority is not None:
                document["standard_authority"] = dict(standard_authority)
            if library_import_surface is not None:
                document["library_import_surface"] = library_import_surface.to_dict()
            evidence_identity = canonical_identity(document).uri
            (evidence / "evidence-manifest.json").write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            digest = canonical_tree_digest(retained)
            require_authority()
            destination = artifact_store.resolve() / digest.removeprefix("sha256:")
            if destination.exists():
                if canonical_tree_digest(destination) != digest:
                    raise BuildError(
                        "builder.mix_cache_conflict",
                        "Mix addressed artifact has conflicting bytes",
                    )
                shutil.rmtree(retained)
            else:
                retained.rename(destination)
            return MixBuildArtifact(
                digest,
                destination,
                request.source_bundle_digest,
                authorization.authorization_id,
                self.toolchain.identity,
                (
                    *self.toolchain.mix.elixir.command,
                    *(
                        token
                        for path in provider_paths
                        for token in (
                            "-pa",
                            str(destination / path.relative_to(retained)),
                        )
                    ),
                    *(
                        token
                        for pkg in packages
                        for token in (
                            "-pa",
                            str(destination / "runtime" / pkg["name"] / "ebin"),
                        )
                    ),
                    *(
                        (str(destination / "source/main.exs"),)
                        if library_import_surface is None
                        else ()
                    ),
                ),
                evidence_identity,
            )
        finally:
            shutil.rmtree(staging)
