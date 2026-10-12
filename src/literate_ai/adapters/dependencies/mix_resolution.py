"""Project freshly verified Mix artifacts into source and resolved BOM custody."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from literate_ai.contracts import ContentIdentity

from .acquisition import _source_package_identity
from .mix_lock import MixLock, MixLockedDependency, MixLockedPackage, render_mix_lock
from .mix_source import MixSourceAuthority, prepare_mix_source_authority
from .types import DependencyObservationError, HostDependencyObservation

if TYPE_CHECKING:
    from literate_ai.adapters.builders.hex import HexToolchain
    from literate_ai.adapters.builders.mix_project import MixBuildArtifact
    from literate_ai.contracts.mix_projects import MixProjectIntent

MIX_INNER_SHA256_PROPERTY = "literate-ai:mix-inner-sha256"
MIX_DEPENDENCY_PROPERTY = "literate-ai:mix-dependency"
_EXACT_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)


@dataclass(frozen=True)
class MixDependencyEvidence:
    """A lifecycle callback's fresh observation, never serialized build metadata.

    The owner must check current target, plan and authorization before invoking
    the artifact observer, including on cache reuse. Historical build grants
    retained in an artifact do not authorize current execution.
    """

    source: MixSourceAuthority
    identity: ContentIdentity
    lock: MixLock
    observation: HostDependencyObservation


class MixDependencyObserver(Protocol):
    source: MixSourceAuthority

    def __call__(self) -> MixDependencyEvidence: ...


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.mix-resolution-invalid", message)


def mix_dependency_property(dependency: MixLockedDependency) -> str:
    """Canonical BOM claim for one native Hex requirement edge."""
    return json.dumps(
        {
            "name": dependency.name,
            "optional": dependency.optional,
            "requirement": dependency.requirement,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def admitted_mix_lock(
    source_content: bytes, *, project: MixProjectIntent
) -> tuple[bytes, MixLock]:
    """Render the framework-owned native lock from exact admitted BOM pins.

    Every Hex component must state its exact ``version``, the outer archive
    SHA-256 in ``hashes``, the inner checksum property and each native
    requirement edge. The rendered lock must close over the project's declared
    dependencies, and every present edge must be a declared BOM edge. Version
    ranges never authorize acquisition: ``mix deps.get`` runs only against this
    lock, so Hex fetches and checksum-checks admitted versions alone.
    """
    try:
        document = json.loads(source_content)
    except (TypeError, ValueError) as exc:
        raise DependencyObservationError(
            "dependencies.mix-pins-required", "Source BOM is not JSON"
        ) from exc
    packages = []
    refs = {}
    for component in document.get("components", []):
        if not isinstance(component, dict):
            continue
        coordinate = _source_package_identity(component)
        if coordinate is None or coordinate[0] != "hex":
            continue
        name = coordinate[1]
        version = component.get("version")
        purl = component.get("purl", "")
        properties = component.get("properties", [])
        if not isinstance(properties, list) or any(
            not isinstance(item, dict) for item in properties
        ):
            properties = None
        inner = (
            [
                item.get("value")
                for item in properties
                if item.get("name") == MIX_INNER_SHA256_PROPERTY
            ]
            if properties is not None
            else []
        )
        hashes = component.get("hashes")
        if (
            name in refs
            or not isinstance(component.get("bom-ref"), str)
            or not isinstance(version, str)
            or _EXACT_VERSION.fullmatch(version) is None
            or ("@" in purl and purl.partition("@")[2] != version)
            or not isinstance(hashes, list)
            or len(hashes) != 1
            or not isinstance(hashes[0], dict)
            or hashes[0].get("alg") != "SHA-256"
            or not isinstance(hashes[0].get("content"), str)
            or re.fullmatch(r"[0-9a-f]{64}", hashes[0]["content"]) is None
            or len(inner) != 1
            or not isinstance(inner[0], str)
            or re.fullmatch(r"[0-9a-f]{64}", inner[0]) is None
        ):
            raise DependencyObservationError(
                "dependencies.mix-pins-required",
                "Mix acquisition requires each source BOM Hex package to pin an "
                "exact version, outer SHA-256 hash and inner checksum; version "
                "ranges never authorize fetching or evaluating dependency code",
            )
        dependencies = []
        for item in properties:
            if item.get("name") != MIX_DEPENDENCY_PROPERTY:
                continue
            try:
                claim = json.loads(item.get("value"))
                if not isinstance(claim, dict) or set(claim) != {
                    "name",
                    "optional",
                    "requirement",
                }:
                    raise ValueError("unexpected Mix dependency claim")
                dependency = MixLockedDependency(
                    claim["name"], claim["requirement"], claim["optional"]
                )
                if not isinstance(dependency.optional, bool) or mix_dependency_property(
                    dependency
                ) != item.get("value"):
                    raise ValueError("non-canonical Mix dependency claim")
            except (TypeError, ValueError) as exc:
                raise DependencyObservationError(
                    "dependencies.mix-pins-required",
                    "Source BOM Mix dependency claims must be canonical JSON",
                ) from exc
            dependencies.append(dependency)
        refs[name] = component["bom-ref"]
        packages.append(
            MixLockedPackage(
                name, version, inner[0], hashes[0]["content"], tuple(dependencies)
            )
        )
    content = render_mix_lock(tuple(packages))
    lock = MixLock.from_bytes(content, project=project)
    edges = {
        (item.get("ref"), child)
        for item in document.get("dependencies", [])
        if isinstance(item, dict)
        for child in item.get("dependsOn", [])
    }
    for parent, child in lock.edges:
        if parent != "@root" and (refs[parent], refs[child]) not in edges:
            raise DependencyObservationError(
                "dependencies.mix-pins-required",
                "Source BOM omits an edge its exact Hex requirements declare",
            )
    return content, lock


def require_admitted_mix_lock(observed: MixLock, admitted: MixLock) -> None:
    """Refuse a native lock that differs from the admitted pins in any respect."""

    def packages(lock):
        return {
            package.name: (
                package.version,
                package.inner_sha256,
                package.outer_sha256,
                frozenset(package.dependencies),
            )
            for package in lock.packages
        }

    if (
        not isinstance(observed, MixLock)
        or not isinstance(admitted, MixLock)
        or packages(observed) != packages(admitted)
        or set(observed.edges) != set(admitted.edges)
    ):
        raise DependencyObservationError(
            "dependencies.mix-lock-not-admitted",
            "Mix acquired a lock that differs from the exact admitted BOM pins",
        )


def validate_mix_source_bom(
    source_content: bytes, *, root_ref: str, source: MixSourceAuthority
) -> None:
    """Require root coordinate and graph coverage of the source BOM.

    This structural check runs before network authorization. Exact pins are
    enforced separately by ``admitted_mix_lock`` before Mix acquires anything.
    """
    document = json.loads(source_content)
    coordinates = {}
    refs = set()
    for component in document.get("components", []):
        coordinate = _source_package_identity(component)
        if coordinate is None or coordinate[0] != "hex":
            continue
        name = coordinate[1]
        ref = component.get("bom-ref")
        purl = component["purl"]
        if (
            name in coordinates
            or not isinstance(ref, str)
            or ref in refs
            or ref == root_ref
            or any(token in purl for token in ("?", "#", "%"))
            or purl.split("@", 1)[0] != f"pkg:hex/{name}"
        ):
            _fail("Source BOM Hex coordinates or references are ambiguous")
        coordinates[name] = ref
        refs.add(ref)
    edges = {
        (item["ref"], child)
        for item in document.get("dependencies", [])
        for child in item.get("dependsOn", [])
    }
    for dependency in source.project.dependencies:
        ref = coordinates.get(dependency.name)
        if ref is None or (root_ref, ref) not in edges:
            _fail("Source BOM omits a declared root Hex coordinate or edge")


def observe_mix_artifact(
    source: MixSourceAuthority,
    artifact: MixBuildArtifact,
    *,
    toolchain: HexToolchain,
    history_verifier: Callable[[Mapping[str, object]], None] | None = None,
) -> MixDependencyEvidence:
    """Recheck acquired archives and retained payloads against independent pins."""
    from literate_ai.adapters.builders.mix_project import _files, verify_mix_artifact
    from literate_ai.adapters.builders.python import canonical_tree_digest

    toolchain.require_unchanged()
    document = verify_mix_artifact(
        artifact, toolchain=toolchain, history_verifier=history_verifier
    )
    retained = _files(artifact.artifact_path)
    try:
        source_files = {
            name: content.decode("utf-8")
            for name, content in retained.items()
            if name.startswith("source/")
        }
    except UnicodeDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.mix-source-invalid", "Retained source is not text"
        ) from exc
    if prepare_mix_source_authority(source_files) != source:
        _fail("Retained Mix source differs from selected source authority")
    lock = MixLock.from_bytes(
        retained[".literate/mix/mix.lock"], project=source.project
    )
    components = []
    for package in lock.packages:
        ref = f"pkg:hex/{package.name}@{package.version}"
        payload = artifact.artifact_path / "runtime" / package.name / "ebin"
        modules = [
            item["module"]
            for item in document["runtime_modules"]
            if item["path"].startswith(f"runtime/{package.name}/ebin/")
        ]
        if not modules:
            _fail("Retained Hex application lacks observed modules")
        components.append(
            {
                "type": "library",
                "bom-ref": ref,
                "name": package.name,
                "version": package.version,
                "purl": ref,
                "hashes": [{"alg": "SHA-256", "content": package.outer_sha256}],
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "package"},
                    {
                        "name": MIX_INNER_SHA256_PROPERTY,
                        "value": package.inner_sha256,
                    },
                    *(
                        {
                            "name": MIX_DEPENDENCY_PROPERTY,
                            "value": mix_dependency_property(dependency),
                        }
                        for dependency in sorted(
                            package.dependencies, key=lambda item: item.name
                        )
                    ),
                    {
                        "name": "literate-ai:mix-runtime-tree-identity",
                        "value": canonical_tree_digest(payload),
                    },
                    *(
                        {"name": "literate-ai:elixir-module", "value": module}
                        for module in sorted(modules)
                    ),
                ],
            }
        )
    refs = {
        "@root": "@root",
        **{p.name: f"pkg:hex/{p.name}@{p.version}" for p in lock.packages},
    }
    # Recheck after reading source and computing each runtime payload identity.
    verify_mix_artifact(
        artifact, toolchain=toolchain, history_verifier=history_verifier
    )
    toolchain.require_unchanged()
    return MixDependencyEvidence(
        source,
        ContentIdentity.parse_uri(artifact.dependency_evidence_identity),
        lock,
        HostDependencyObservation(
            tuple(components), tuple((refs[a], refs[b]) for a, b in lock.edges)
        ),
    )


def project_mix_evidence(
    source_content: bytes,
    *,
    root_ref: str,
    source: MixSourceAuthority,
    evidence: MixDependencyEvidence,
) -> HostDependencyObservation:
    """Require exact Hex closure, source ranges, hashes and source graph coverage."""
    from .resolution import _require_version_in_range

    if (
        not isinstance(evidence, MixDependencyEvidence)
        or evidence.source != source
        or not isinstance(evidence.identity, ContentIdentity)
        or not isinstance(evidence.lock, MixLock)
        or not isinstance(evidence.observation, HostDependencyObservation)
    ):
        _fail("Mix resolution lacks source-bound acquired payload evidence")
    validate_mix_source_bom(source_content, root_ref=root_ref, source=source)
    expected = {package.name: package for package in evidence.lock.packages}
    observed = {}
    observation_refs = {"@root": "@root"}
    for component in evidence.observation.components:
        coordinate = _source_package_identity(component)
        if coordinate is None or coordinate[0] != "hex":
            _fail("Mix payload contains an unexpected package coordinate")
        name = coordinate[1]
        package = expected.get(name)
        ref = component.get("bom-ref")
        if (
            package is None
            or name in observed
            or not isinstance(ref, str)
            or ref in observation_refs
            or component.get("version") != package.version
            or component.get("hashes")
            != [{"alg": "SHA-256", "content": package.outer_sha256}]
        ):
            _fail("Mix acquired inventory differs from the exact native lock")
        observed[name] = component
        observation_refs[ref] = name
    if set(observed) != set(expected):
        _fail("Mix payload omits locked packages")
    try:
        edges = {
            (observation_refs[a], observation_refs[b])
            for a, b in evidence.observation.edges
        }
    except KeyError:
        _fail("Mix observation graph references an unknown package")
    if edges != set(evidence.lock.edges):
        _fail("Mix observed graph differs from its native lock")
    document = json.loads(source_content)
    refs = {"@root": root_ref}
    projected = []
    for component in document.get("components", []):
        coordinate = _source_package_identity(component)
        if coordinate is None or coordinate[0] != "hex":
            continue
        name = coordinate[1]
        ref = component.get("bom-ref")
        if (
            name not in observed
            or name in refs
            or not isinstance(ref, str)
            or ref in refs.values()
        ):
            _fail("Source BOM has an unobserved or ambiguous Hex package")
        package = expected[name]
        purl = component["purl"]
        if "@" in purl and purl.partition("@")[2] != package.version:
            _fail("Source Hex PURL version differs from the native lock")
        if "version" in component:
            if component["version"] != package.version:
                _fail("Source BOM exact Hex version differs from the native lock")
        else:
            _require_version_in_range(component.get("versionRange"), package.version)
        if "hashes" in component and component["hashes"] != observed[name]["hashes"]:
            _fail("Source BOM Hex checksum differs from acquired bytes")
        for key in (
            MIX_INNER_SHA256_PROPERTY,
            MIX_DEPENDENCY_PROPERTY,
            "literate-ai:mix-runtime-tree-identity",
            "literate-ai:elixir-module",
        ):
            claimed = {
                item["value"]
                for item in component.get("properties", [])
                if item.get("name") == key
            }
            actual = {
                item["value"]
                for item in observed[name].get("properties", [])
                if item.get("name") == key
            }
            if not claimed <= actual:
                _fail("Source BOM claims absent or different Hex runtime evidence")
        refs[name] = ref
        projected.append({**observed[name], "bom-ref": ref, "name": component["name"]})
    if set(refs) != {"@root", *expected}:
        _fail("Source BOM omits an acquired Hex package")
    projected_edges = {(refs[a], refs[b]) for a, b in edges}
    source_edges = {
        (item["ref"], child)
        for item in document.get("dependencies", [])
        for child in item.get("dependsOn", [])
    }
    if not projected_edges <= source_edges:
        _fail("Source BOM omits a native Mix dependency edge")
    return HostDependencyObservation(tuple(projected), tuple(sorted(projected_edges)))
