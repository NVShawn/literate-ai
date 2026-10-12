"""Reopen native Mix provenance from bounded exports without host execution."""

from __future__ import annotations

import hashlib
import json
import re

from literate_ai.adapters.builders.mix_project import _require_metadata
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.dependencies.hex_archive import verify_hex_archive
from literate_ai.adapters.dependencies.mix_lock import MixLock
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildPlan,
    StandardProjectLifecycleError,
)
from literate_ai.contracts import (
    ContentIdentity,
    LibraryImportSurface,
    canonical_identity,
)
from literate_ai.contracts.elixir_libraries import elixir_namespace
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.mix_projects import MixProjectIntent
from literate_ai.security import AuthorizationError, BuildAuthorization, BuildRequest


def mix_target_document(plan):
    return {
        "schema": "literate-ai/standard-mix-target@1",
        "component_revision": plan.component_revision.uri,
        "build_system_resolver_identity": (
            plan.request.build_system_resolver_identity.uri
        ),
        "hex_toolchain_identity": plan.request.build_system_toolchain_identity.uri,
        "manifest": "source/mix-project.json",
    }


def is_mix_request(plan, request):
    """Recognize only the independently planned, exact Standard Mix target."""
    return request.builder_id == canonical_identity(mix_target_document(plan)).uri


def verify_qualification_mix_build(
    reader, *, plan, observation, files, exports, provider_exports, current_commands
):
    from literate_ai.adapters.qualification_capture import QualificationCaptureError

    def mismatch():
        raise QualificationCaptureError("qualification.capture.mix-build-mismatch")

    def same(actual, expected):
        if canonical_json_bytes(actual) != canonical_json_bytes(expected):
            mismatch()

    def read_grant(identity):
        return BuildAuthorization.from_dict(reader.read_json(identity))

    try:
        keys = {
            "schema",
            "builder_id",
            "request",
            "authorization",
            "source_bundle_digest",
            "request_identity",
            "authorization_id",
            "toolchain_identity",
            "project",
            "lock_edges",
            "acquired_archives",
            "native_metadata",
            "runtime_modules",
            "provider_libraries",
            "provider_layout",
            "phases",
            "files",
            "standard_authority",
        }
        if set(observation) not in (keys, keys | {"library_import_surface"}):
            mismatch()
        if (
            observation["schema"] != "literate-ai/mix-build-evidence@1"
            or observation["builder_id"] != "builder:elixir-mix@1"
        ):
            mismatch()
        authority = observation["standard_authority"]
        if (
            set(authority)
            != {
                "schema",
                "target_identity",
                "build_plan_identity",
                "source_tree_identity",
                "source_bundle_identity",
                "provider_source_inputs",
            }
            or authority["schema"] != "literate-ai/standard-mix-build@1"
        ):
            mismatch()
        origin_id = ContentIdentity.parse_uri(authority["build_plan_identity"])
        origin = StandardComponentBuildPlan.from_dict(reader.read_json(origin_id))
        if origin.identity != origin_id:
            mismatch()
        # A sealed cache may retain a previous issued plan/grant. Its provenance
        # must reopen exactly; it never becomes the current consumer's authority.
        historical = BuildRequest.from_dict(observation["request"])
        grant = BuildAuthorization.from_dict(observation["authorization"])
        grant.require_valid(historical, now=grant.issued_at)
        same(
            grant.to_dict(), read_grant(origin.request.authorization_identity).to_dict()
        )
        current_grant = read_grant(plan.request.authorization_identity)
        current_request = BuildRequest.from_dict(
            reader.read_json(ContentIdentity.parse_uri(current_grant.request_digest))
        )
        same(historical.to_dict(), current_request.to_dict())
        same(mix_target_document(origin), mix_target_document(plan))
        contract = current_commands.get(plan.component_revision)
        if (
            contract is None
            or contract.component_revision != plan.component_revision
            or contract.locked_build_authority_identity.uri != historical.builder_id
            or contract.build_system_toolchain_identity
            != plan.request.build_system_toolchain_identity
            or contract.language_compiler_identity
            != plan.request.language_compiler_identity
            or contract.language_runtime_identity
            != plan.request.language_runtime_identity
        ):
            mismatch()
        same(
            observation.get("library_import_surface"),
            contract.library_import_surface.to_dict() if contract.is_library else None,
        )
        if (
            not is_mix_request(plan, historical)
            or observation["request_identity"]
            != canonical_identity(historical.to_dict()).uri
            or observation["authorization_id"] != grant.authorization_id
            or observation["toolchain_identity"] != historical.toolchain_digest
            or historical.toolchain_digest
            != plan.request.build_system_toolchain_identity.uri
            or plan.manifest.build_system_driver_identity.uri
            != historical.toolchain_digest
            or historical.effective_revision_digest != plan.component_revision.uri
            or authority["target_identity"] != historical.builder_id
            or authority["source_tree_identity"]
            != origin.request.source_tree_identity.uri
            or origin.request.source_tree_identity != plan.request.source_tree_identity
            or authority["source_bundle_identity"] != historical.source_bundle_digest
            or observation["source_bundle_digest"] != historical.source_bundle_digest
            or historical.sandbox_profile != "local-explicit-host-process"
            or historical.requested_privileges
            != ("execute-build-tools", "network-access")
        ):
            mismatch()
        if len(exports) != 1 or len(plan.manifest.export_declarations) != 1:
            mismatch()
        export = exports[0]
        declaration = plan.manifest.export_declarations[0]
        if (
            export.export_id != declaration.export_id
            or export.component_revision != plan.component_revision
        ):
            mismatch()
        selected_exports = {}
        pending = list(origin.provider_artifact_identities)
        while pending:
            identity = pending.pop()
            if identity in selected_exports:
                continue
            provider = provider_exports.get(identity)
            if (
                provider is None
                or provider.identity != identity
                or provider.role != "library"
                or provider.target_identity != export.target_identity
                or provider.toolchain_identity
                != plan.request.language_compiler_identity
            ):
                mismatch()
            selected_exports[identity] = provider
            pending.extend(provider.dependency_artifact_identities)
        if (
            origin.provider_artifact_identities != plan.provider_artifact_identities
            or export.dependency_artifact_identities
            != plan.provider_artifact_identities
            or {
                ContentIdentity.parse_uri(item["artifact_identity"])
                for item in authority["provider_source_inputs"]
            }
            != set(selected_exports)
        ):
            mismatch()
        content = reader.read_bytes(ContentIdentity.parse_uri(export.blob.identity))
        members = read_directory_export(
            content, export.blob, max_bytes=export.blob.size, max_entries=len(files)
        )
        payload = {member.path: member.content for member in members}
        prefix = export.export_id + "/"
        same(
            [
                {"path": prefix + name, "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(payload.items())
            ],
            [item for item in files if item["path"].startswith(prefix)],
        )
        same(
            json.loads(payload.pop(".literate/mix/evidence-manifest.json")), observation
        )
        same(
            observation["files"],
            [
                {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(payload.items())
            ],
        )
        project = MixProjectIntent.from_bytes(payload["source/mix-project.json"])
        same(project.to_dict(), observation["project"])
        lock = MixLock.from_bytes(payload[".literate/mix/mix.lock"], project=project)
        same([list(edge) for edge in lock.edges], observation["lock_edges"])
        same(
            [
                verify_hex_archive(
                    payload[f".literate/mix/{package.name}-{package.version}.tar"],
                    package=package,
                ).to_dict()
                for package in lock.packages
            ],
            observation["acquired_archives"],
        )
        _require_metadata(observation["native_metadata"], lock)

        def directory(prefix, pin, application=None):
            data = {
                name.removeprefix(prefix): content
                for name, content in payload.items()
                if name.startswith(prefix)
            }
            if not data or any(
                "/" in name or (not name.endswith(".beam") and name != application)
                for name in data
            ):
                mismatch()
            digest = canonical_identity(
                [
                    {
                        "path": name,
                        "size": len(content),
                        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    }
                    for name, content in sorted(data.items())
                ]
            ).uri
            if digest != pin or (application and application not in data):
                mismatch()
            return [prefix + name for name in sorted(data) if name.endswith(".beam")]

        records = observation["provider_libraries"]
        inputs = authority["provider_source_inputs"]
        if (
            not isinstance(records, list)
            or not isinstance(inputs, list)
            or len(records) != len(inputs)
        ):
            mismatch()
        layout = observation["provider_layout"]
        if layout not in {"ordinal-v1", "digest-v1"}:
            mismatch()
        identities, applications, provider_paths, surfaces = set(), {}, [], []
        for index, (record, source) in enumerate(zip(records, inputs, strict=True)):
            base_keys = {"artifact_identity", "import_surface", "tree_digest"}
            if set(record) not in (base_keys, base_keys | {"applications"}):
                mismatch()
            identity = ContentIdentity.parse_uri(record["artifact_identity"])
            if identity in identities or source["artifact_identity"] != identity.uri:
                mismatch()
            identities.add(identity)
            surface = LibraryImportSurface.from_dict(record["import_surface"])
            if surface.language != "elixir":
                mismatch()
            same(surface.to_dict(), source["import_surface"])
            original = selected_exports[identity]
            provider_contract = current_commands.get(original.component_revision)
            if (
                provider_contract is None
                or not provider_contract.is_library
                or provider_contract.component_revision != original.component_revision
            ):
                mismatch()
            same(surface.to_dict(), provider_contract.library_import_surface.to_dict())
            source_payload = {
                member.path: member.content
                for member in read_directory_export(
                    reader.read_bytes(
                        ContentIdentity.parse_uri(original.blob.identity)
                    ),
                    original.blob,
                    max_bytes=original.blob.size,
                    max_entries=65534,
                )
            }
            source_prefix = (
                f"source/{surface.package}/"
                if "source_tree_digest" in source
                else f"runtime/{surface.package}/ebin/"
            )
            source_tree = canonical_identity(
                [
                    {
                        "path": name.removeprefix(source_prefix),
                        "size": len(data),
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    }
                    for name, data in sorted(source_payload.items())
                    if name.startswith(source_prefix)
                ]
            ).uri
            if "source_tree_digest" in source:
                if (
                    set(source)
                    != {"artifact_identity", "import_surface", "source_tree_digest"}
                    or source_tree != source["source_tree_digest"]
                ):
                    mismatch()
            else:
                same(source, record)
                if source_tree != record["tree_digest"]:
                    mismatch()
                original_project = MixProjectIntent.from_bytes(
                    source_payload["source/mix-project.json"]
                )
                original_lock = MixLock.from_bytes(
                    source_payload[".literate/mix/mix.lock"], project=original_project
                )
                expected_apps = [
                    (original_project.app, original_project.version),
                    *(
                        (package.name, package.version)
                        for package in original_lock.packages
                    ),
                ]
                if [
                    (app["name"], app["version"])
                    for app in record.get("applications", [])
                ] != expected_apps:
                    mismatch()
                for app in record["applications"]:
                    app_prefix = f"runtime/{app['name']}/ebin/"
                    pin = canonical_identity(
                        [
                            {
                                "path": name.removeprefix(app_prefix),
                                "size": len(data),
                                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                            }
                            for name, data in sorted(source_payload.items())
                            if name.startswith(app_prefix)
                        ]
                    ).uri
                    if pin != app["tree_digest"]:
                        mismatch()
            slot = str(index) if layout == "ordinal-v1" else identity.digest
            base = f"providers/{slot}/"
            apps = record.get("applications", [])
            if not isinstance(apps, list) or ("applications" in record and not apps):
                mismatch()
            own_paths = directory(
                base + "ebin/",
                record["tree_digest"],
                surface.package + ".app" if apps else None,
            )
            if not apps:
                provider_paths.extend(own_paths)
            names = set()
            for app_index, app in enumerate(apps):
                if (
                    set(app) != {"name", "version", "tree_digest"}
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", app["name"])
                    or not re.fullmatch(
                        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?",
                        app["version"],
                    )
                ):
                    mismatch()
                name = app["name"]
                if name in names or (
                    app_index == 0
                    and (
                        name != surface.package
                        or app["tree_digest"] != record["tree_digest"]
                    )
                ):
                    mismatch()
                names.add(name)
                ebin = base + (
                    "ebin/" if app_index == 0 else f"applications/{name}/ebin/"
                )
                paths = directory(ebin, app["tree_digest"], name + ".app")
                pin = (app["version"], app["tree_digest"])
                if name in applications and applications[name] != pin:
                    mismatch()
                if name not in applications:
                    provider_paths.extend(paths)
                applications[name] = pin
            surfaces.append((surface, own_paths))
        paths = list(provider_paths)
        for name, version in (
            (project.app, project.version),
            *((package.name, package.version) for package in lock.packages),
        ):
            prefix = f"runtime/{name}/ebin/"
            data = {
                path.removeprefix(prefix): content
                for path, content in payload.items()
                if path.startswith(prefix)
            }
            pin = canonical_identity(
                [
                    {
                        "path": path,
                        "size": len(content),
                        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    }
                    for path, content in sorted(data.items())
                ]
            ).uri
            modules = directory(prefix, pin, name + ".app")
            if name in applications:
                if applications[name] != (version, pin):
                    mismatch()
            else:
                paths.extend(modules)
        modules = observation["runtime_modules"]
        if (
            not isinstance(modules, list)
            or [item["path"] for item in modules] != paths
            or any(
                set(item) != {"module", "path"}
                or not isinstance(item["module"], str)
                or not item["module"]
                for item in modules
            )
            or len({item["module"] for item in modules}) != len(modules)
        ):
            mismatch()
        if "library_import_surface" in observation:
            own_surface = LibraryImportSurface.from_dict(
                observation["library_import_surface"]
            )
            if own_surface.language != "elixir" or own_surface.package != project.app:
                mismatch()
            surfaces.append(
                (
                    own_surface,
                    [
                        path
                        for path in paths
                        if path.startswith(f"runtime/{project.app}/ebin/")
                    ],
                )
            )
        for surface, selected in surfaces:
            namespace = "Elixir." + elixir_namespace(surface.package)
            if any(
                item["path"] in selected
                and not (
                    item["module"] == namespace
                    or item["module"].startswith(namespace + ".")
                )
                for item in modules
            ):
                mismatch()
        phases = observation["phases"]
        expected = (
            []
            if not records
            else ["provider-module-inventory"]
            + (["provider-application-verification"] if applications else [])
        )
        expected += [
            "deps-get",
            "dependency-verification",
            "compile",
            "package",
            "retained-module-inventory",
            "retained-runtime-verification",
        ]
        if "library_import_surface" in observation:
            expected.append("library-interface-verification")
        if records:
            expected.append("provider-interface-verification")
        if (
            not isinstance(phases, list)
            or [item["phase"] for item in phases] != expected
        ):
            mismatch()
        streams = {}
        for phase in phases:
            if (
                set(phase) != {"phase", "returncode", "stdout_sha256", "stderr_sha256"}
                or type(phase["returncode"]) is not int
                or phase["returncode"] != 0
            ):
                mismatch()
            for stream in ("stdout", "stderr"):
                data = payload[f".literate/mix/process/{phase['phase']}.{stream}"]
                if hashlib.sha256(data).hexdigest() != phase[stream + "_sha256"]:
                    mismatch()
                if stream == "stdout":
                    streams[phase["phase"]] = data
        if {name for name in payload if name.startswith(".literate/mix/process/")} != {
            f".literate/mix/process/{phase}.{stream}"
            for phase in expected
            for stream in ("stdout", "stderr")
        }:
            mismatch()
        same(
            json.loads(streams["dependency-verification"]),
            observation["native_metadata"],
        )
        inventory = json.loads(streams["retained-module-inventory"])
        same(inventory, json.loads(streams["retained-runtime-verification"]))
        if len(inventory) != len(modules):
            mismatch()
        roots = set()
        for actual, module in zip(inventory, modules, strict=True):
            if (
                set(actual) != {"path", "module"}
                or actual["module"] != module["module"]
            ):
                mismatch()
            path = actual["path"].replace("\\", "/")
            suffix = "/" + module["path"]
            if not path.endswith(suffix):
                mismatch()
            roots.add(path[: -len(suffix)])
        if len(roots) != 1:
            mismatch()
        if records:
            provider_inventory = json.loads(streams["provider-module-inventory"])
            same(provider_inventory, inventory[: len(provider_paths)])
            if applications:
                same(
                    provider_inventory,
                    json.loads(streams["provider-application-verification"]),
                )
        if "library_import_surface" in observation:
            same(
                json.loads(streams["library-interface-verification"]),
                [[cap.capability for cap in own_surface.capabilities]],
            )
        if records:
            same(
                json.loads(streams["provider-interface-verification"]),
                [
                    [cap.capability for cap in surface.capabilities]
                    for surface, _ in surfaces[: len(records)]
                ],
            )
    except QualificationCaptureError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        BuildError,
        AuthorizationError,
        StandardProjectLifecycleError,
        DependencyObservationError,
    ) as exc:
        raise QualificationCaptureError(
            "qualification.capture.mix-build-mismatch"
        ) from exc
