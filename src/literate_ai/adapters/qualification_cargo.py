"""Reopen native Cargo build observations without tools or workspace extraction."""

from __future__ import annotations

import json
import tomllib

from literate_ai.adapters.compiler_cache import validate_compiler_cache_observation
from literate_ai.adapters.lifecycle.standard_cargo import StandardCargoTarget
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes


def verify_qualification_cargo_build(reader, *, plan, observation, files) -> None:
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
    if {path for path in artifact_files if path.startswith(prefix)} != {
        prefix + "Cargo.lock",
        prefix + "metadata.json",
        prefix + "evidence-manifest.json",
    }:
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
