"""Project freshly verified Mix artifacts into source and resolved BOM custody."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from literate_ai.contracts import ContentIdentity

from .acquisition import _source_package_identity
from .mix_lock import MixLock
from .mix_source import MixSourceAuthority, prepare_mix_source_authority
from .types import DependencyObservationError, HostDependencyObservation

if TYPE_CHECKING:
    from literate_ai.adapters.builders.hex import HexToolchain
    from literate_ai.adapters.builders.mix_project import MixBuildArtifact


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


def validate_mix_source_bom(
    source_content: bytes, *, root_ref: str, source: MixSourceAuthority
) -> None:
    """Require root coordinate and graph coverage before network authorization."""
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
                        "name": "literate-ai:mix-inner-sha256",
                        "value": package.inner_sha256,
                    },
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
            "literate-ai:mix-inner-sha256",
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
