"""Observe exact SDK native images without loading their implementation.

OS/architecture and loader closure are necessary evidence, not a proof of language
ABI, API conformance, consumer acceptance, or permission to execute the SDK.
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from literate_ai.adapters.dependencies.observation import (
    PortableHostDependencyObserver,
    _file_digest,
    _stable_file_bytes,
)
from literate_ai.adapters.native_sdk_custody import capture_native_sdk
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_sdks import NativeSdkSnapshot
from literate_ai.storage.cas import FileSystemCAS

_ARCHITECTURES = {"x86_64", "arm64"}
_OPERATING_SYSTEMS = {"linux", "macos", "windows"}
_PATH_PROPERTIES = {
    "linux": "literate-ai:elf-path",
    "macos": "literate-ai:macho-path",
    "windows": "literate-ai:pe-path",
}


def require_native_sdk_target(operating_system: str, architecture: str) -> None:
    if operating_system not in _OPERATING_SYSTEMS or architecture not in _ARCHITECTURES:
        raise ValueError("SDK native target requires a supported exact OS/architecture")


def _pe_architecture(path: Path) -> str:
    # Machine is the first COFF field after the PE signature; values are defined by
    # https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#machine-types
    dos = _stable_file_bytes(path, limit=64)
    if len(dos) != 64 or dos[:2] != b"MZ":
        raise ValueError("SDK runtime image has no complete PE DOS header")
    offset = int.from_bytes(dos[60:64], "little")
    if offset < 64:
        raise ValueError("SDK runtime image has an invalid PE header offset")
    header = _stable_file_bytes(path, offset=offset, limit=24)
    if len(header) != 24 or header[:4] != b"PE\0\0":
        raise ValueError("SDK runtime image has no complete PE COFF header")
    machine = int.from_bytes(header[4:6], "little")
    try:
        return {0x8664: "x86_64", 0xAA64: "arm64"}[machine]
    except KeyError as exc:
        raise ValueError("SDK runtime PE machine type is unsupported") from exc


def _architectures(properties: list[dict[str, str]], operating_system: str) -> set[str]:
    values = {item["name"]: item["value"] for item in properties}
    if operating_system == "linux":
        architecture = values.get("literate-ai:elf-architecture")
        return {
            "ELF64/Advanced Micro Devices X86-64": {"x86_64"},
            "ELF64/AArch64": {"arm64"},
        }.get(architecture, set())
    if operating_system == "macos":
        aliases = {
            "arm64": "arm64",
            "arm64e": "arm64",
            "x86_64": "x86_64",
            "x86_64h": "x86_64",
        }
        return {
            aliases[arch]
            for name in values
            if name.startswith("literate-ai:macho-uuid:")
            and (arch := name.removeprefix("literate-ai:macho-uuid:")) in aliases
        }
    return {_pe_architecture(Path(values[_PATH_PROPERTIES[operating_system]]))}


def observe_native_sdk_runtime(
    snapshot: NativeSdkSnapshot,
    *,
    expected_identity: ContentIdentity,
    target_identity: ContentIdentity,
    operating_system: str,
    architecture: str,
    root: Path,
    store: FileSystemCAS,
) -> BlobRef:
    """Retain current native-loader evidence over the exact SDK bytes and target."""
    require_native_sdk_target(operating_system, architecture)
    if (
        snapshot.identity != expected_identity
        or snapshot.target_identity != target_identity
    ):
        raise ValueError(
            "SDK runtime observation differs from the requested exact inputs"
        )
    host_os = {"darwin": "macos", "win32": "windows", "cygwin": "windows"}.get(
        sys.platform, sys.platform
    )
    host_arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    if (host_os, host_arch) != (operating_system, architecture):
        raise ValueError("SDK runtime observation requires the selected target host")

    def require_bytes() -> None:
        actual = capture_native_sdk(
            root,
            store=store,
            source_lock_identity=snapshot.source_lock_identity,
            recipe_identity=snapshot.recipe_identity,
            target_identity=snapshot.target_identity,
            license_identity=snapshot.license_identity,
            import_surface=snapshot.import_surface,
            import_root=snapshot.import_root,
            native_libraries=snapshot.native_libraries,
        )
        if actual.identity != expected_identity:
            raise ValueError("SDK bytes changed before or during runtime observation")

    require_bytes()
    root_ref = expected_identity.uri
    observed = PortableHostDependencyObserver().observe(
        {"artifact_path": str(root)}, root_ref=root_ref
    )
    components = {item["bom-ref"]: item for item in observed.components}
    if len(components) != len(observed.components) or root_ref in components:
        raise ValueError(
            "SDK runtime observation contains ambiguous component identities"
        )
    graph: dict[str, set[str]] = {ref: set() for ref in (*components, root_ref)}
    for source, target in observed.edges:
        if source not in graph or target not in components:
            raise ValueError(
                "SDK runtime observation has an unresolved dependency edge"
            )
        graph[source].add(target)

    def closure(seeds: set[str]) -> set[str]:
        seen: set[str] = set()
        pending = list(seeds)
        while pending:
            ref = pending.pop()
            if ref not in seen:
                seen.add(ref)
                pending.extend(graph[ref] - seen)
        return seen

    if closure({root_ref}) != {*components, root_ref}:
        raise ValueError("SDK runtime observation contains disconnected dependencies")
    path_property = _PATH_PROPERTIES[operating_system]
    native_by_path: dict[str, list[str]] = {}
    for ref, component in components.items():
        for item in component.get("properties", ()):
            if item["name"] == path_property:
                native_by_path.setdefault(item["value"], []).append(ref)
    files = {item.path: item.blob for item in snapshot.files}
    bindings = []
    for relative in snapshot.native_libraries:
        refs = native_by_path.get(str((root / relative).resolve(strict=True)), [])
        if len(refs) != 1:
            raise ValueError(
                "SDK native library is absent or ambiguous in the observed closure"
            )
        ref = refs[0]
        if {"alg": "SHA-256", "content": files[relative].digest} not in components[
            ref
        ].get("hashes", ()):
            raise ValueError("SDK native library hash differs from the observed image")
        bindings.append({"path": relative, "component": ref})
    runtime_refs = closure({binding["component"] for binding in bindings})
    verified_native_refs: set[str] = set()
    sdk_native_refs = {
        ref
        for path, refs in native_by_path.items()
        if Path(path).resolve().is_relative_to(root.resolve(strict=True))
        for ref in refs
    }
    for ref in runtime_refs:
        properties = components[ref].get("properties", [])
        if any(item["name"] == path_property for item in properties):
            if architecture not in _architectures(properties, operating_system):
                raise ValueError(
                    "SDK native image architecture differs from the selected target"
                )
            values = {item["name"]: item["value"] for item in properties}
            hashes = components[ref].get("hashes", ())
            if hashes:
                path = Path(values[path_property])
                resolved = values.get("literate-ai:macho-resolved-path", str(path))
                if str(path.resolve(strict=True)) != resolved:
                    raise ValueError(
                        "SDK runtime library path changed during observation"
                    )
                actual_hash = _file_digest(Path(resolved)).removeprefix("sha256:")
                if {"alg": "SHA-256", "content": actual_hash} not in hashes:
                    raise ValueError(
                        "SDK runtime library bytes changed during observation"
                    )
                verified_native_refs.add(ref)
    _require_conditional_import_evidence(
        components,
        graph,
        runtime_refs,
        sdk_native_refs,
        verified_native_refs,
        operating_system=operating_system,
    )
    require_bytes()
    return store.put_manifest(
        {
            "schema": "literate-ai/native-sdk-runtime-observation@1",
            "snapshot_identity": snapshot.identity.to_dict(),
            "target_identity": target_identity.to_dict(),
            "operating_system": operating_system,
            "architecture": architecture,
            "native_libraries": bindings,
            "components": list(observed.components),
            "edges": [list(edge) for edge in observed.edges],
        }
    )


def _require_conditional_import_evidence(
    components,
    graph,
    runtime_refs: set[str],
    sdk_native_refs: set[str],
    verified_native_refs: set[str],
    *,
    operating_system: str,
) -> None:
    """Keep unavailable external delay imports as conditions, never SDK contents.

    Windows may load these imports only when their functions are called. Preserve
    the complete observation; current command/API acceptance, not this inspection,
    must establish whether the selected SDK operation can use the host environment.
    """
    for ref in runtime_refs:
        properties = components[ref].get("properties", [])
        values = {item["name"]: item["value"] for item in properties}
        unavailable = {
            item["value"].casefold()
            for item in properties
            if item["name"] == "literate-ai:pe-delay-import-unavailable"
        }
        if unavailable:
            if (
                operating_system != "windows"
                or ref in sdk_native_refs
                or ref not in verified_native_refs
            ):
                raise ValueError(
                    "SDK runtime closure contains an unavailable delayed import"
                )
            if not unavailable <= _declared_delayed_imports(properties):
                raise ValueError(
                    "SDK delayed import lacks conditional inspection evidence"
                )
        if values.get("literate-ai:pe-api-set-host-available") == "false":
            importers = {source for source in runtime_refs if ref in graph[source]}
            contract = values.get("literate-ai:pe-api-set-contract")
            if (
                operating_system != "windows"
                or values.get("literate-ai:pe-delay-import-only") != "true"
                or not importers
                or importers & sdk_native_refs
                or not importers <= verified_native_refs
                or not isinstance(contract, str)
                or not contract
                or any(
                    contract.casefold()
                    not in _declared_delayed_imports(
                        components[source].get("properties", [])
                    )
                    for source in importers
                )
            ):
                raise ValueError(
                    "SDK runtime closure contains an unavailable API-set host"
                )


def _declared_delayed_imports(properties) -> set[str]:
    delayed: set[str] = set()
    required: set[str] = set()
    for item in properties:
        if item["name"] != "literate-ai:pe-import-edge":
            continue
        edge = json.loads(item["value"])
        if (
            not isinstance(edge, dict)
            or set(edge) != {"name", "delay_load"}
            or not isinstance(edge["name"], str)
            or not edge["name"]
            or type(edge["delay_load"]) is not bool
        ):
            raise ValueError("SDK delayed import has invalid inspection evidence")
        (delayed if edge["delay_load"] else required).add(edge["name"].casefold())
    return delayed - required
