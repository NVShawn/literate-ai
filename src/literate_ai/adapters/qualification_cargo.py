"""Reopen native Cargo build observations without tools or workspace extraction."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import PurePosixPath, PureWindowsPath

from literate_ai.adapters.compiler_cache import validate_compiler_cache_observation
from literate_ai.adapters.lifecycle.standard_cargo import StandardCargoTarget
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes


def verify_qualification_cargo_build(
    reader,
    *,
    plan,
    observation,
    files,
    provider_exports=None,
    current_commands=None,
    component_lock=None,
    source_candidate=None,
    realized_exports=None,
) -> None:
    """Bind all native phases and dependency bytes to the recorded build plan."""

    from literate_ai.adapters.qualification_capture import QualificationCaptureError

    def mismatch():
        raise QualificationCaptureError("qualification.capture.cargo-build-mismatch")

    def same(actual, expected):
        if canonical_json_bytes(actual) != canonical_json_bytes(expected):
            mismatch()

    def identity(value):
        return ContentIdentity.parse_uri(value)

    target_id = identity(observation["target_identity"])
    raw = reader.read_json(target_id)
    target = StandardCargoTarget(
        component_revision=identity(raw["component_revision"]),
        build_system_resolver_identity=identity(raw["build_system_resolver_identity"]),
        build_system_toolchain_identity=identity(
            raw["build_system_toolchain_identity"]
        ),
        language_compiler_identity=identity(raw["language_compiler_identity"]),
        manifest=raw["manifest"],
        binary=raw["binary"],
        rustc_command=tuple(raw["rustc_command"]),
        library=raw.get("library", False),
    )
    same(raw, target.identity_document())
    grant = reader.read_json(plan.request.authorization_identity)
    request = reader.read_json(identity(grant["request_digest"]))
    if (
        request["builder_id"] != target_id.uri
        or target.component_revision != plan.component_revision
        or target.build_system_resolver_identity
        != plan.request.build_system_resolver_identity
        or target.build_system_toolchain_identity
        != plan.request.build_system_toolchain_identity
        or target.build_system_toolchain_identity
        != plan.manifest.build_system_driver_identity
        or target.language_compiler_identity != plan.request.language_compiler_identity
    ):
        mismatch()
    processes = {}
    for name, phase in (
        ("generate_lockfile", "cargo-generate-lockfile"),
        ("metadata", "cargo-metadata"),
        ("build", "cargo-build"),
    ):
        process_id = identity(observation[name])
        value = reader.read_json(process_id)
        stdout = identity(value["stdout_identity"])
        stderr = identity(value["stderr_identity"])
        same(
            value,
            {
                "schema": "literate-ai/local-cargo-process-observation@1",
                "phase": phase,
                "plan_identity": plan.identity.uri,
                "returncode": 0,
                "stdout_identity": stdout.uri,
                "stderr_identity": stderr.uri,
            },
        )
        processes[name] = reader.read_bytes(stdout)
        reader.read_bytes(stderr)
    same(
        observation,
        {
            "schema": "literate-ai/local-cargo-build-observation@1",
            **{name: observation[name] for name in processes},
            "target_identity": target_id.uri,
            **(
                {
                    "compiler_cache": validate_compiler_cache_observation(
                        observation["compiler_cache"]
                    )
                }
                if "compiler_cache" in observation
                else {}
            ),
        },
    )
    artifact_files = {item["path"]: "sha256:" + item["sha256"] for item in files}
    prefix = ".literate/cargo/"
    native_path = prefix + "native-provider-graph.json"
    projected = any(".literate/native-cargo/" in path for path in artifact_files)
    native = None
    if (
        projected
        or native_path in artifact_files
        or plan.provider_artifact_identities
        or any(
            value is not None
            for value in (component_lock, current_commands, provider_exports)
        )
    ):
        from literate_ai.adapters.qualification_native_cargo import (
            reopen_native_cargo_inputs,
        )

        native = reopen_native_cargo_inputs(
            reader,
            plan=plan,
            component_lock=component_lock,
            current_commands=current_commands,
            provider_exports=provider_exports,
            target=target,
        )
    expected_paths = {
        prefix + "Cargo.lock",
        prefix + "metadata.json",
        prefix + "evidence-manifest.json",
    }
    if native is not None:
        expected_paths.add(native_path)
    if {path for path in artifact_files if path.startswith(prefix)} != expected_paths:
        mismatch()
    manifest = reader.read_json(
        identity(artifact_files[prefix + "evidence-manifest.json"])
    )
    lock_id = identity(artifact_files[prefix + "Cargo.lock"])
    metadata_id = identity(artifact_files[prefix + "metadata.json"])
    same(
        manifest,
        {
            "schema": "urn:literate-ai:schema:v1:standard-cargo-dependency-evidence",
            "authorization_id": plan.request.authorization_identity.uri,
            "builder_id": "literate-ai/standard-cargo-lifecycle@1",
            "component_revision": plan.component_revision.uri,
            "build_plan_identity": plan.identity.uri,
            "source_bundle_digest": plan.request.source_tree_identity.uri,
            "build_toolchain_identity": target.build_system_toolchain_identity.uri,
            "language_compiler_identity": target.language_compiler_identity.uri,
            "target_identity": target_id.uri,
            "lock_identity": lock_id.uri,
            "metadata_identity": metadata_id.uri,
            "files": [
                {"path": prefix + "Cargo.lock", "digest": lock_id.uri},
                {"path": prefix + "metadata.json", "digest": metadata_id.uri},
                *(
                    [{"path": native_path, "digest": artifact_files[native_path]}]
                    if native is not None
                    else []
                ),
            ],
        },
    )
    lock = tomllib.loads(reader.read_bytes(lock_id).decode("utf-8"))
    metadata_bytes = reader.read_bytes(metadata_id)
    metadata = json.loads(metadata_bytes)
    if canonical_json_bytes(json.loads(processes["metadata"])) != metadata_bytes:
        mismatch()

    def packages(value):
        if not isinstance(value, list) or not value:
            mismatch()
        result = set()
        for package in value:
            if not isinstance(package, dict) or any(
                not isinstance(package.get(key), str) or not package[key]
                for key in ("name", "version")
            ):
                mismatch()
            result.add((package["name"], package["version"]))
        return result

    if packages(lock["package"]) != packages(metadata["packages"]):
        mismatch()

    if native is not None:
        _verify_native_evidence(
            reader,
            plan=plan,
            target=target,
            artifact_files=artifact_files,
            native=native,
            lock_bytes=reader.read_bytes(lock_id),
            metadata=metadata,
            mismatch=mismatch,
            same=same,
            source_candidate=source_candidate,
            contract=current_commands[plan.component_revision],
            realized_exports=realized_exports,
        )


def _verify_native_evidence(
    reader,
    *,
    plan,
    target,
    artifact_files,
    native,
    lock_bytes,
    metadata,
    mismatch,
    same,
    source_candidate,
    contract,
    realized_exports,
):
    """Recompute native producer evidence using inert historical coordinates."""
    from literate_ai.adapters.lifecycle.native_cargo_inputs import (
        validate_native_cargo_provider_locks,
    )
    from literate_ai.adapters.lifecycle.native_cargo_projection import (
        MAX_MANIFEST_BYTES,
        MAX_PROJECTION_BYTES,
        MAX_PROJECTION_FILES,
        project_native_cargo_graph,
        validate_native_cargo_metadata,
    )

    graph, sources = native
    reserved = ".literate/native-cargo/"
    from literate_ai.adapters.qualification_capture import (
        verify_qualification_source_records,
    )
    from literate_ai.contracts.executable_components.packages import SourceBundleFile

    if source_candidate is None or (
        source_candidate.component_revision != plan.component_revision
        or source_candidate.tree_identity != plan.request.source_tree_identity
    ):
        mismatch()
    verify_qualification_source_records(reader, candidate=source_candidate)
    bundle = reader.read_json(source_candidate.source_bundle_identity)
    source_files = tuple(SourceBundleFile.from_dict(item) for item in bundle["files"])
    # Full source verification above applies the existing bounded CAS contract.
    # This new join reads only the exact independently bound original manifest.
    manifests = tuple(row for row in source_files if row.path == target.manifest)
    if len(manifests) != 1 or manifests[0].blob.size > MAX_MANIFEST_BYTES:
        mismatch()
    for row in source_files:
        path = row.path.casefold()
        if (
            ".cargo" in path.split("/")
            or path.startswith(".literate/native-cargo")
            or path
            == str(PurePosixPath(target.manifest).with_name("build.rs")).casefold()
        ):
            mismatch()
    original = reader.read_bytes(ContentIdentity.parse_uri(manifests[0].blob.identity))
    rewritten, projection = project_native_cargo_graph(
        graph=graph,
        root_manifest=original,
        root_manifest_path=target.manifest,
        sources=sources,
    )
    if target.library:
        export_prefix = contract.artifact_export.export_id + "/"
        from literate_ai.adapters.directory_artifacts import read_directory_export

        exports = tuple(
            export
            for export in realized_exports
            if export.export_id == contract.artifact_export.export_id
            and export.component_revision == plan.component_revision
            and export.source_tree_identity == plan.request.source_tree_identity
            and export.dependency_artifact_identities
            == plan.provider_artifact_identities
            and export.role == "library"
            and export.target_identity == contract.artifact_export.target_identity
        )
        if len(exports) != 1:
            mismatch()
        export = exports[0]
        exported = read_directory_export(
            reader.read_bytes(
                ContentIdentity.parse_uri("sha256:" + export.blob.digest)
            ),
            export.blob,
            max_bytes=MAX_PROJECTION_BYTES,
            max_entries=MAX_PROJECTION_FILES,
        )
        if {
            export_prefix + row.path: "sha256:"
            + hashlib.sha256(row.content).hexdigest()
            for row in exported
        } != {
            path: identity
            for path, identity in artifact_files.items()
            if path.startswith(export_prefix)
        }:
            mismatch()
        selected = {
            row.path: row.content
            for row in exported
            if row.path == target.manifest or row.path.startswith(reserved)
        }
        if (
            len(selected) > MAX_PROJECTION_FILES
            or sum(map(len, selected.values())) > MAX_PROJECTION_BYTES
            or selected.get(target.manifest) != rewritten
            or {
                path: raw for path, raw in selected.items() if path.startswith(reserved)
            }
            != {row.path: row.content for row in projection}
        ):
            mismatch()
    projection_root = _historical_projection_root(metadata, target.manifest, mismatch)
    binding = validate_native_cargo_metadata(
        graph=graph,
        root_manifest=original,
        root_manifest_path=target.manifest,
        sources=sources,
        projection_root=projection_root,
        metadata=metadata,
        current_paths=False,
    )
    locks = validate_native_cargo_provider_locks(
        graph=graph,
        sources=sources,
        consumer_manifest=original,
        consumer_lock=lock_bytes,
    )
    same(
        reader.read_json(
            ContentIdentity.parse_uri(
                artifact_files[".literate/cargo/native-provider-graph.json"]
            )
        ),
        {
            "schema": "literate-ai/standard-native-cargo-evidence@1",
            "graph": graph.to_dict(),
            "original_manifest_identity": "sha256:"
            + hashlib.sha256(original).hexdigest(),
            "metadata_binding": binding,
            "provider_locks": locks,
        },
    )


def _historical_projection_root(metadata, manifest, mismatch):
    """Select inert canonical coordinates; never resolve/stat/open these paths."""
    root_id = metadata["resolve"]["root"]
    roots = [row for row in metadata["packages"] if row.get("id") == root_id]
    if len(roots) != 1 or type(roots[0].get("manifest_path")) is not str:
        mismatch()
    raw_path = roots[0]["manifest_path"]
    path_type = PureWindowsPath if PureWindowsPath(raw_path).drive else PurePosixPath
    path = path_type(raw_path)
    suffix = PurePosixPath(manifest).parts
    if (
        not path.is_absolute()
        or "\0" in raw_path
        or (
            path_type is PurePosixPath
            and ("\\" in raw_path or raw_path.startswith("//"))
        )
        or str(path) != raw_path
        or ".." in path.parts
        or path.parts[-len(suffix) :] != suffix
    ):
        mismatch()
    return path.parents[len(suffix) - 1]
