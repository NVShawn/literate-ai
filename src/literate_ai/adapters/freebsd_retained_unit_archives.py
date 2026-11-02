"""Native archive production while the original retained owner remains live.

The canonical composed projection owns topology. This producer streams accepted
physical files one at a time and formats explicit link headers with libarchive;
it never stages a dependency's bytes inside an alias-only unit. Complete-set
verification and current retained/SDK owners are mandatory before returning.
"""

from __future__ import annotations

import hashlib
import io
import platform
import tarfile
import tempfile
from pathlib import Path

from literate_ai.adapters import freebsd_packaging as native
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.freebsd_composed_tree_packaging import (
    verify_freebsd_composed_package_set,
)
from literate_ai.adapters.freebsd_package_elf import inspect_freebsd_package_elf
from literate_ai.adapters.native_tar_archives import rewrite_native_tar
from literate_ai.adapters.native_tree_shards import retain_native_tree
from literate_ai.adapters.retained_project_inputs import _stream
from literate_ai.adapters.retained_unit_graphs import retain_retained_unit_graph
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.retained_unit_packages import RetainedUnitPackageResult


class _BoundedTarOutput:
    def __init__(self, output, maximum):
        self.output, self.maximum = output, maximum

    def write(self, block):
        if self.output.tell() + len(block) > self.maximum:
            raise PackagingError("Native unit TAR exceeds its staging budget")
        count = self.output.write(block)
        if count != len(block):
            raise PackagingError("Native unit TAR staging write was incomplete")
        return count

    def tell(self):
        return self.output.tell()

    def flush(self):
        self.output.flush()


def _header(path, item=None):
    member = tarfile.TarInfo(path)
    member.uname, member.gname = "root", "wheel"
    if item is None:
        member.mode = 0o644
        return member
    member.uid = member.gid = 65534
    member.mtime, member.mode = native._EPOCH, item.mode
    member.type = {
        "file": tarfile.REGTYPE,
        "directory": tarfile.DIRTYPE,
        "symlink": tarfile.SYMTYPE,
        "hardlink": tarfile.LNKTYPE,
    }[item.kind]
    return member


def _manifest_path(path):
    # pkg manifests URL-decode keys. Match native pkg's encoding of literal
    # percent signs and non-ASCII UTF-8 bytes; TAR paths remain unchanged.
    return "".join(
        f"%{byte:02X}" if byte == 37 or byte >= 128 else chr(byte)
        for byte in path.encode("utf-8")
    )


def _unit_manifests(projection, identity, expected, store, work):
    """Bound aggregate records and ELF names before growing their containers."""
    compact = projection.unit_manifest(identity)
    base_bytes = len(canonical_json_bytes(compact))
    if base_bytes > native._MAX_METADATA:
        raise PackagingError("Native unit intent exceeds manifest byte policy")
    files, directories, required, provided = {}, {}, set(), set()
    record_bytes, library_bytes, flatsize = 64, 0, 0
    scratch = work / "physical"
    for path, item in expected.items():
        if item.kind == "directory":
            record = {"uname": "root", "gname": "wheel", "perm": "0755"}
        else:
            if item.kind == "file":
                checksum = item.blob.digest
                store.copy_to(item.blob, scratch)
                try:
                    with scratch.open("rb") as stream:
                        needs, supplies = inspect_freebsd_package_elf(
                            stream, item.blob.size, projection.pkg_tool.abi
                        )
                    for names, found in ((required, needs), (provided, supplies)):
                        for name in found:
                            if name not in names:
                                library_bytes += len(canonical_json_bytes(name)) + 1
                                if base_bytes + record_bytes + library_bytes > (
                                    native._MAX_METADATA
                                ):
                                    raise PackagingError(
                                        "Native unit ELF inventory exceeds "
                                        "manifest policy"
                                    )
                                names.add(name)
                finally:
                    scratch.unlink()
                flatsize += item.blob.size
            elif item.kind == "symlink":
                text = item.target.encode("utf-8")
                checksum = hashlib.sha256(text).hexdigest()
                flatsize += len(text)
            else:
                # Canonical composed geometry resolves every alias chain to a
                # physical entry. Referencing its digest copies no target bytes.
                target = projection.composition._entries.get(item.target)
                if target is None or target.kind != "file" or target.blob is None:
                    raise PackagingError("Native unit hardlink has no physical owner")
                checksum = target.blob.digest
            record = {
                "sum": checksum,
                "uname": "root",
                "gname": "wheel",
                "perm": f"{item.mode:04o}",
                "mtime": native._EPOCH,
            }
            if item.kind == "symlink":
                record["symlink_target"] = item.target
        key = _manifest_path(path)
        record_bytes += (
            len(canonical_json_bytes(key)) + len(canonical_json_bytes(record)) + 2
        )
        if base_bytes + record_bytes + library_bytes > native._MAX_METADATA:
            raise PackagingError("Native unit records exceed manifest byte policy")
        (directories if item.kind == "directory" else files)[key] = record
    compact.update(
        flatsize=flatsize,
        shlibs_required=sorted(required - provided),
        shlibs_provided=sorted(provided),
    )
    full = {**compact, "files": files, "directories": directories}
    documents = tuple(canonical_json_bytes(value) for value in (compact, full))
    if any(len(value) > native._MAX_METADATA for value in documents):
        raise PackagingError("Native unit manifest exceeds byte policy")
    return documents


def _write_unit_tar(projection, identity, expected, store, work, maximum):
    compact, full = _unit_manifests(projection, identity, expected, store, work)
    source, scratch = work / "canonical.tar", work / "physical"
    prefix = projection._parameters[identity].prefix
    with source.open("xb") as output:
        sink = _BoundedTarOutput(output, maximum)
        with tarfile.open(fileobj=sink, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, document in (("+COMPACT_MANIFEST", compact), ("+MANIFEST", full)):
                header = _header(name)
                header.size = len(document)
                archive.addfile(header, io.BytesIO(document))
            for path, item in expected.items():
                header = _header(path, item)
                header.linkname = (
                    prefix + "/" + item.target
                    if item.kind == "hardlink"
                    else item.target or ""
                )
                if item.kind == "file":
                    header.size = item.blob.size
                    store.copy_to(item.blob, scratch)
                    try:
                        with scratch.open("rb") as stream:
                            archive.addfile(header, stream)
                    finally:
                        scratch.unlink()
                else:
                    archive.addfile(header)
    size, _mode, digest = _stream(source.parent, source.name, maximum)
    return source, BlobRef(digest, size, media_type="application/x-tar")


def construct_retained_unit_archives(owner, *, object_root):
    """Construct the full selected set; parsed plans cannot enter this path."""
    from literate_ai.adapters.retained_project_unit_packages import (
        RetainedProjectUnitPackages,
    )

    if not isinstance(owner, RetainedProjectUnitPackages):
        raise TypeError("Native unit production requires the live retained owner")
    if platform.system().casefold() != "freebsd":
        raise PackagingError("Native unit package production requires FreeBSD")
    owner.require_current()
    owner.admission.require_native_package_scope()
    owner.pkg_tool.require_unchanged()
    owner.tar_tool.require_current()
    projection, store = owner.projection, owner.admission.store
    parent = Path(object_root).absolute()
    directory_node(parent)
    if 4096 * projection.graph.index.unit_count > projection.max_provider_memory_bytes:
        raise PackagingError("Native unit result inventory exceeds memory policy")
    for identity in projection._definitions:
        expected = projection.unit_members(identity)
        largest = max(
            (item.blob.size for item in expected.values() if item.blob is not None),
            default=0,
        )
        if (
            max(
                projection.unit_expansion_limit(identity)
                + largest
                + projection.set_plan.max_archive_bytes,
                2 * projection.set_plan.max_archive_bytes,
            )
            > projection.set_plan.max_staging_bytes
        ):
            raise PackagingError("Native unit construction exceeds staging budget")
    # These are immutable retained assertions, not a success/admission record.
    for reference, content in owner._metadata.items():
        if store.put_bytes(content, media_type=reference.media_type) != reference:
            raise PackagingError("Native unit metadata identity changed")
    if (
        retain_native_tree(projection.graph.tree, store)
        != projection.set_plan.tree_index
        or retain_retained_unit_graph(projection.graph, store)
        != projection.set_plan.graph_index
    ):
        raise PackagingError("Native unit graph/tree retention changed")
    results, total = [], 0
    for identity in projection.graph.partition.verification_order():
        remaining = min(
            projection.set_plan.max_archive_bytes,
            projection.set_plan.max_total_archive_bytes - total,
            projection.set_plan.max_transport_bytes - total,
        )
        if remaining < 1:
            raise PackagingError("Native unit set exceeds archive/transport budget")
        # Intermediate CAS values are assertions, never admitted unit successes.
        # Revalidate the complete retained/SDK scope before and after the entire
        # operation, rather than rehashing every input once per selected unit.
        owner.pkg_tool.require_unchanged()
        with tempfile.TemporaryDirectory(
            prefix="retained-unit-pkg-", dir=parent
        ) as raw:
            work = Path(raw)
            source, reference = _write_unit_tar(
                projection,
                identity,
                projection.unit_members(identity),
                store,
                work,
                projection.unit_expansion_limit(identity),
            )
            destination = work / "unit.pkg"
            with destination.open("x+b") as output:
                rewrite_native_tar(
                    owner.tar_tool,
                    source,
                    reference,
                    output,
                    maximum_input_bytes=projection.unit_expansion_limit(identity),
                    maximum_output_bytes=remaining,
                    maximum_expanded_bytes=projection.unit_expansion_limit(identity),
                )
            size = destination.stat().st_size
            if not 0 < size <= remaining:
                raise PackagingError("Native unit set exceeds archive/transport budget")
            # The input belongs to this operation and the native writer has
            # finished rechecking it. Retire it before CAS makes its own bounded
            # archive copy; preflight separately reserves both archive copies.
            source.unlink()
            archive = store.put_file(
                destination, media_type="application/vnd.freebsd.pkg"
            )
            total += archive.size
            if total > min(
                projection.set_plan.max_total_archive_bytes,
                projection.set_plan.max_transport_bytes,
            ):
                raise PackagingError("Native unit set exceeds archive/transport budget")
            plan = projection.unit_plan(identity)
            result = RetainedUnitPackageResult(
                plan.identity,
                projection.set_plan.identity,
                identity,
                projection.graph.identity,
                projection.set_plan.packager_identity,
                projection.set_plan.tool_identity,
                archive,
            )
            store.put_manifest(result.to_dict(), media_type="application/json")
            results.append(result)
    results = tuple(sorted(results, key=lambda value: value.definition_identity.uri))
    verify_freebsd_composed_package_set(
        projection, results, store=store, object_root=parent
    )
    owner.require_current()
    owner.admission.require_native_package_scope()
    owner.pkg_tool.require_unchanged()
    owner.tar_tool.require_current()
    return results
