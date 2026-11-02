"""Portable archive assertions for byte-inspection controls, never live producers."""

import hashlib
import io
import json
import lzma
import tarfile
from pathlib import Path

from literate_ai.adapters.freebsd_composed_tree_packaging import (
    FreeBsdComposedPackageSetProjection,
    composed_freebsd_packager_identity,
    composed_freebsd_policy,
    composed_freebsd_tool_identity,
)
from literate_ai.adapters.freebsd_retained_unit_parameters import (
    FreeBsdRetainedUnitParameters,
)
from literate_ai.adapters.native_tar_archives import NativeTarToolBinding
from literate_ai.adapters.native_tree_shards import (
    prepare_native_tree,
    retain_native_tree,
)
from literate_ai.adapters.retained_unit_graphs import (
    prepare_retained_unit_graph,
    retain_retained_unit_graph,
)
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
)
from literate_ai.contracts.identity import (
    ComponentCoordinate,
    ContentIdentity,
    canonical_identity,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_unit_packages import (
    RetainedUnitDependency,
    RetainedUnitOwnership,
    RetainedUnitPackageDefinition,
    RetainedUnitPackageResult,
    RetainedUnitPackageSetPlan,
)
from literate_ai.contracts.retained_unit_partition import RetainedUnitPartition


def composed_fixture(tree, store, pkg_tool, *, record_cache_bytes=1024**3):
    limits = RetainedProjectLimits(500, 65536, 16 * 1024**2)
    tree = prepare_native_tree(
        tree.directories, tree.files, tree.aliases, limits=limits
    )
    tree_ref = retain_native_tree(tree, store)
    roles = {
        "component-authoring": "component_authoring_identity",
        "component-lock": "component_lock_identity",
        "retained-project-profile": "recipe_identity",
        "retained-source-manifest": "retained_source_identity",
        "cyclonedx-source-sbom": "source_bom_identity",
        "cyclonedx-resolved-sbom": "resolved_bom_identity",
        "retained-receipt": "receipt_identity",
        "license": "license_identity",
    }
    definitions, parameters = {}, {}
    for name in ("physical", "aliases"):
        blobs = {
            field: store.put_manifest(
                {
                    "fixture": field,
                    "owner": name
                    if field
                    in {
                        "component_authoring_identity",
                        "component_lock_identity",
                        "request_identity",
                    }
                    else "project",
                },
                media_type="application/json",
            )
            for field in RetainedUnitPackageDefinition.IDENTITY_FIELDS
        }
        identities = {
            field: ContentIdentity.parse_uri(blob.identity)
            for field, blob in blobs.items()
        }
        identities["payload_identity"] = tree.identity
        prefix = ".literate-ai/units/" + name + "/"
        evidence = [
            PackageInput(
                prefix + role + ".json",
                role,
                PackageFileKind.RESOURCE,
                identities[field],
                identities["target_identity"],
                blobs[field],
            )
            for role, field in roles.items()
        ]
        specification = store.put_bytes(
            b"Mechanical fixture specification " + name.encode()
        )
        evidence.append(
            PackageInput(
                prefix + "component.md",
                "component-specification",
                PackageFileKind.RESOURCE,
                identities["component_authoring_identity"],
                identities["target_identity"],
                specification,
            )
        )
        params = FreeBsdRetainedUnitParameters(
            "fixture-" + name,
            "devel/fixture-" + name,
            "/usr/local/lib/retained-fixture",
            pkg_tool.architecture,
            tuple(sorted(evidence, key=lambda value: value.path)),
        )
        reference = store.put_manifest(params.to_dict(), media_type="application/json")
        definitions[name] = RetainedUnitPackageDefinition(
            name,
            ComponentCoordinate("fixture", name),
            **identities,
            provider_parameters=reference,
            abi=pkg_tool.abi,
            version="1.0",
        )
        parameters[name] = params
    physical, aliases = definitions["physical"], definitions["aliases"]
    selected = tuple(sorted(definitions.values(), key=lambda value: value.identity.uri))
    partition = RetainedUnitPartition(
        tree,
        selected,
        (
            RetainedUnitOwnership("bin/cc", "file", physical.identity),
            RetainedUnitOwnership("bin/compiler", "hardlink", aliases.identity),
            RetainedUnitOwnership("empty", "empty-directory", physical.identity),
            RetainedUnitOwnership("public", "symlink", aliases.identity),
        ),
        (
            RetainedUnitDependency(
                aliases.identity,
                physical.identity,
                "tree-link",
                physical.target_identity,
            ),
        ),
        limits,
        10,
        20,
        65536,
    )
    graph = prepare_retained_unit_graph(
        partition,
        tree_ref,
        max_descriptor_bytes=2400,
        max_descriptor_total_bytes=65536,
        max_index_bytes=4096,
        max_metadata_memory_bytes=1024**2,
    )
    graph_ref = retain_retained_unit_graph(graph, store)
    tar_tool = NativeTarToolBinding(
        Path("/unavailable/bsdtar"), "1" * 64, "bsdtar 3.5.3 - libarchive 3.7.4"
    )
    tools = composed_freebsd_tool_identity(pkg_tool, tar_tool)
    set_plan = RetainedUnitPackageSetPlan(
        physical.retained_plan_identity,
        physical.receipt_identity,
        physical.target_identity,
        graph_ref,
        tree_ref,
        canonical_identity(
            composed_freebsd_policy(
                1000, 1024**2, max_verified_record_cache_bytes=record_cache_bytes
            )
        ),
        composed_freebsd_packager_identity(tools),
        tools,
        1,
        128 * 1024**2,
        32 * 1024**2,
        1024**2,
        2 * 1024**2,
    )
    projection = FreeBsdComposedPackageSetProjection(
        set_plan,
        graph,
        tuple(parameters[value.unit_name] for value in selected),
        pkg_tool,
        tar_tool,
        1000,
        1024**2,
        max_verified_record_cache_bytes=record_cache_bytes,
    )
    return projection, definitions


def archive_assertion(projection, identity, store, mutate=None):
    """Build an independent format fixture, not evidence of native construction."""
    members = projection.unit_members(identity)
    params = projection._parameters[identity]
    physical = {
        params.prefix + "/" + value.path: value
        for value in projection.composition.members
        if value.kind == "file"
    }
    records, directories, bodies = {}, {}, {}
    flat = 0
    for path, item in members.items():
        if item.kind == "directory":
            directories[path] = {"uname": "root", "gname": "wheel", "perm": "0755"}
            continue
        data = (
            item.target.encode()
            if item.kind == "symlink"
            else store.get_bytes(item.blob)
            if item.kind == "file"
            else store.get_bytes(physical[params.prefix + "/" + item.target].blob)
        )
        records[path] = {
            "sum": hashlib.sha256(data).hexdigest(),
            "uname": "root",
            "gname": "wheel",
            "perm": f"{item.mode:04o}",
            "mtime": 946684800,
        }
        if item.kind == "symlink":
            records[path]["symlink_target"] = item.target
        bodies[path] = data if item.kind == "file" else b""
        flat += len(data) if item.kind in {"file", "symlink"} else 0
    compact = {**projection.unit_manifest(identity), "flatsize": flat}
    full = {**compact, "files": records, "directories": directories}
    entries = []
    for name, value in (("+COMPACT_MANIFEST", compact), ("+MANIFEST", full)):
        body = json.dumps(value).encode()
        member = tarfile.TarInfo(name)
        member.mode, member.size, member.uname, member.gname = (
            0o644,
            len(body),
            "root",
            "wheel",
        )
        entries.append((member, body))
    for path, item in members.items():
        member = tarfile.TarInfo(path)
        member.uid = member.gid = 65534
        member.uname, member.gname, member.mode, member.mtime = (
            "root",
            "wheel",
            item.mode,
            946684800,
        )
        member.type = {
            "file": tarfile.REGTYPE,
            "directory": tarfile.DIRTYPE,
            "symlink": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
        }[item.kind]
        member.linkname = (
            params.prefix + "/" + item.target
            if item.kind == "hardlink"
            else item.target or ""
        )
        body = bodies.get(path, b"")
        member.size = len(body)
        entries.append((member, body))
    if mutate:
        entries = mutate(entries)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member, body in entries:
            archive.addfile(member, io.BytesIO(body))
    blob = store.put_bytes(
        lzma.compress(output.getvalue()), media_type="application/vnd.freebsd.pkg"
    )
    unit_plan = projection.unit_plan(identity)
    return RetainedUnitPackageResult(
        unit_plan.identity,
        projection.set_plan.identity,
        identity,
        projection.graph.identity,
        projection.set_plan.packager_identity,
        projection.set_plan.tool_identity,
        blob,
    )
