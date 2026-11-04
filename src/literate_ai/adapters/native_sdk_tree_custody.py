"""No-follow SDK tree custody; neither capture nor relocation issues admission."""

from __future__ import annotations

import os
import stat
import tempfile
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.retained_project_inputs import (
    _directory_access,
    _inventory,
    _link_bytes,
    _remove_tree,
    _require_case_sensitive,
    _root,
    _stamp,
    _stream,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.native_sdk_tree_products import (
    NativeSdkTreeBuildLayout,
    NativeSdkTreeSnapshot,
)
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias, NativeSdkTree
from literate_ai.contracts.native_sdks import NativeSdkFile
from literate_ai.contracts.native_tree_shards import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_TOTAL_BYTES,
    MAX_INDEX_BYTES,
    MAX_TREE_MEMBERS,
    ComposedNativeTree,
    validate_native_tree_policy,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.storage.cas import FileSystemCAS


def capture_native_sdk_tree_snapshot(
    root: Path,
    *,
    layout: NativeSdkTreeBuildLayout,
    store: FileSystemCAS,
    source_lock_identity: ContentIdentity,
    recipe_identity: ContentIdentity,
    target_identity: ContentIdentity,
) -> NativeSdkTreeSnapshot:
    """Bind captured tree bytes to explicit layout and claimed producer inputs."""
    if not isinstance(layout, NativeSdkTreeBuildLayout):
        raise TypeError("SDK tree capture requires an explicitly selected tree layout")
    tree = capture_native_sdk_tree(
        root, store=store, limits=layout.limits, case_sensitive=layout.case_sensitive
    )
    snapshot = NativeSdkTreeSnapshot(
        source_lock_identity,
        recipe_identity,
        target_identity,
        layout.license_identity,
        layout.import_surface,
        layout.import_root,
        layout.native_libraries,
        tree,
    )
    if (
        snapshot.file_at(layout.output_license).blob.identity
        != layout.license_identity.uri
    ):
        raise ValueError("SDK tree output does not retain the selected license bytes")
    return snapshot


def materialize_native_sdk_tree_snapshot(
    snapshot: NativeSdkTreeSnapshot,
    *,
    expected_identity: ContentIdentity,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
    parent: Path,
) -> Path:
    """Relocate an exact snapshot; claimed provenance still needs live admission."""
    if (
        not isinstance(snapshot, NativeSdkTreeSnapshot)
        or snapshot.identity != expected_identity
    ):
        raise ValueError("SDK tree snapshot differs from the requested exact identity")
    return materialize_native_sdk_tree(
        snapshot.tree,
        expected_identity=snapshot.tree.identity,
        store=store,
        limits=limits,
        parent=parent,
    )


def capture_native_sdk_tree(
    root: Path,
    *,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
    case_sensitive: bool = False,
) -> NativeSdkTree:
    return _capture_native_tree(
        root,
        store=store,
        limits=limits,
        case_sensitive=case_sensitive,
        maximum_entries=16384,
        tree_factory=NativeSdkTree,
    )


def capture_composed_native_tree(
    root: Path,
    *,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
    case_sensitive: bool = False,
    max_descriptor_bytes=MAX_DESCRIPTOR_BYTES,
    max_descriptor_total_bytes=MAX_DESCRIPTOR_TOTAL_BYTES,
    max_index_bytes=MAX_INDEX_BYTES,
    max_metadata_memory_bytes=1024 * 1024 * 1024,
) -> ComposedNativeTree:
    """Capture larger composed geometry through the same no-follow custody owner."""
    return _capture_composed_native_tree(
        root,
        store=store,
        limits=limits,
        case_sensitive=case_sensitive,
        max_descriptor_bytes=max_descriptor_bytes,
        max_descriptor_total_bytes=max_descriptor_total_bytes,
        max_index_bytes=max_index_bytes,
        max_metadata_memory_bytes=max_metadata_memory_bytes,
    )


def _capture_composed_native_tree(
    root: Path,
    *,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
    case_sensitive: bool = False,
    max_descriptor_bytes=MAX_DESCRIPTOR_BYTES,
    max_descriptor_total_bytes=MAX_DESCRIPTOR_TOTAL_BYTES,
    max_index_bytes=MAX_INDEX_BYTES,
    max_metadata_memory_bytes=1024 * 1024 * 1024,
    output_custody=None,
    reserved_memory_bytes=0,
    budget_capture=False,
    retain_payloads=True,
) -> ComposedNativeTree:
    """Capture larger composed geometry through the same no-follow custody owner."""
    from literate_ai.adapters.native_tree_shards import prepare_native_tree

    validate_native_tree_policy(
        limits,
        case_sensitive,
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= max_metadata_memory_bytes
        or type(budget_capture) is not bool
        or type(retain_payloads) is not bool
    ):
        raise ValueError("tree.shards.invalid-memory-reservation")
    budget_capture = (
        budget_capture or output_custody is not None or bool(reserved_memory_bytes)
    )
    baseline = 16 * (max_descriptor_bytes + max_index_bytes)
    capture_memory = {"inventory": 0, "tree": 0, "pending": baseline}
    if baseline + reserved_memory_bytes >= max_metadata_memory_bytes:
        raise ValueError("tree.shards.metadata-memory-policy-exceeded")

    def inventory_policy():
        count = descriptor_bound = 0

        def observe(name, metadata):
            nonlocal count, descriptor_bound
            count += 1
            # Conservative wire-space allowance before metadata retention or
            # payload streaming. Exact descriptor geometry is checked separately
            # after inode grouping, still before any payload is copied.
            row_bound = len(canonical_json_bytes(name)) + 1
            if stat.S_ISREG(metadata.st_mode):
                row_bound += len(
                    canonical_json_bytes(
                        NativeSdkFile(
                            name,
                            BlobRef("0" * 64, metadata.st_size),
                            bool(metadata.st_mode & 0o111),
                        ).to_dict()
                    )
                )
            elif stat.S_ISLNK(metadata.st_mode):
                if metadata.st_size > 4096:
                    raise ValueError("SDK capture symbolic target exceeds byte bound")
                row_bound += 128 + 6 * metadata.st_size
            descriptor_bound += row_bound
            if descriptor_bound > max_descriptor_total_bytes:
                raise ValueError("tree.shards.capture-descriptor-policy-exceeded")
            estimate = baseline + 16 * descriptor_bound + 2048 * count
            retained = (
                capture_memory["inventory"] + capture_memory["tree"]
                if budget_capture
                else 0
            )
            if reserved_memory_bytes + retained + estimate > max_metadata_memory_bytes:
                raise ValueError("tree.shards.metadata-memory-policy-exceeded")
            capture_memory["pending"] = estimate

        return observe

    def compose(directories, files, aliases, case_sensitive):
        return prepare_native_tree(
            directories,
            files,
            aliases,
            limits=limits,
            case_sensitive=case_sensitive,
            max_descriptor_bytes=max_descriptor_bytes,
            max_descriptor_total_bytes=max_descriptor_total_bytes,
            max_index_bytes=max_index_bytes,
            max_metadata_memory_bytes=max_metadata_memory_bytes,
            reserved_memory_bytes=(
                reserved_memory_bytes + capture_memory["inventory"]
                if budget_capture
                else 0
            ),
        )

    return _capture_native_tree(
        root,
        store=store,
        limits=limits,
        case_sensitive=case_sensitive,
        maximum_entries=min(
            MAX_TREE_MEMBERS,
            (max_metadata_memory_bytes - baseline - reserved_memory_bytes) // 2048,
        ),
        tree_factory=compose,
        inventory_policy=inventory_policy,
        preflight_geometry=True,
        output_custody=output_custody,
        reserved_memory_bytes=reserved_memory_bytes,
        capture_memory=capture_memory if budget_capture else None,
        retain_payloads=retain_payloads,
    )


def _capture_native_tree(
    root,
    *,
    store,
    limits,
    case_sensitive,
    maximum_entries,
    tree_factory,
    inventory_policy=None,
    preflight_geometry=False,
    output_custody=None,
    reserved_memory_bytes=0,
    capture_memory=None,
    retain_payloads=True,
):
    """Capture physical bytes and link topology through existing input custody.

    Bounds charge canonical file bytes and symbolic target bytes. Hard links
    must form complete inode groups within this root. Input/source/recipe claims
    and actual completed-producer admission belong to the enclosing SDK owner.
    """
    if (
        not isinstance(limits, RetainedProjectLimits)
        or type(case_sensitive) is not bool
    ):
        raise TypeError("SDK capture requires typed limits and boolean case selection")
    root = _root(Path(root))
    if type(retain_payloads) is not bool or (not retain_payloads and store is not None):
        raise TypeError("hash-only tree observation requires no content store")
    if retain_payloads and store.root.is_relative_to(root):
        raise ValueError("SDK content store must be outside the captured tree")
    # Only the separate HTML output owner can issue this finite observation.
    # Old SDK callers have no exclusion selection and preserve complete capture.
    if output_custody is not None:
        from .html_tree_output_initialization import _html_tree_capture_exclusions

        excluded_roots, excluded_files, revalidate_output = (
            _html_tree_capture_exclusions(
                output_custody,
                root,
                reserved_memory_bytes=(
                    reserved_memory_bytes
                    + (
                        0
                        if capture_memory is None
                        else (capture_memory["inventory"] + capture_memory["tree"])
                    )
                ),
            )
        )
    else:
        excluded_roots = excluded_files = frozenset()
        revalidate_output = None

    def revalidate_owned_output(additional_memory_bytes=0):
        if revalidate_output is not None:
            retained = (
                capture_memory["inventory"] + capture_memory["tree"]
                if capture_memory is not None
                else 0
            )
            revalidate_output(
                reserved_memory_bytes=(
                    reserved_memory_bytes + retained + additional_memory_bytes
                )
            )

    revalidate_owned_output()
    with _directory_access(root) as access:

        def top_names():
            descriptor = None
            if access is not None:
                descriptor = os.open(
                    ".",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=access.directory(())[0],
                )
            result = []
            try:
                with os.scandir(
                    descriptor if descriptor is not None else root
                ) as entries:
                    for entry in entries:
                        if len(result) >= min(limits.max_entries, maximum_entries):
                            raise ValueError("SDK capture exceeds entry bound")
                        result.append(entry.name)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            return tuple(sorted(result))

        roots = top_names()

        def inventory():
            revalidate_owned_output()
            result = _inventory(
                root,
                roots,
                min(limits.max_entries, maximum_entries),
                allow_internal_symlinks=True,
                access=access,
                observe=inventory_policy() if inventory_policy is not None else None,
                excluded_roots=excluded_roots,
                excluded_regular_files=excluded_files,
            )
            revalidate_owned_output(
                0 if capture_memory is None else capture_memory["pending"]
            )
            return result

        def metadata(name):
            path = root / name
            if access is not None:
                return access.metadata(path)
            require_safe_directory(path.parent)
            return path.lstat()

        names, directories = inventory()
        if capture_memory is not None:
            capture_memory["inventory"] = capture_memory["pending"]
        before = {name: metadata(name) for name in names}
        groups = defaultdict(list)
        aliases = []
        total = 0
        for name, value in before.items():
            if stat.S_ISLNK(value.st_mode):
                target = _link_bytes(root, name, 4096, access).decode("utf-8")
                total += len(target.encode("utf-8"))
                aliases.append(NativeSdkAlias(name, target))
            elif stat.S_ISREG(value.st_mode):
                groups[value.st_dev, value.st_ino].append(name)
            else:
                raise ValueError("SDK capture contains an unsupported member")
        files = []
        if total > limits.max_total_bytes:
            raise ValueError("SDK capture exceeds total byte bound")
        if preflight_geometry:
            # Stat-derived placeholders have identical encoded size to the
            # eventual SHA-256 refs. They remain local planning values, are never
            # retained, and cannot become a producer/admission claim.
            planned_files, planned_aliases = [], list(aliases)
            for group in groups.values():
                owner = min(group)
                value = before[owner]
                planned_files.append(
                    NativeSdkFile(
                        owner,
                        BlobRef("0" * 64, value.st_size),
                        bool(value.st_mode & 0o111),
                    )
                )
                planned_aliases.extend(
                    NativeSdkAlias(name, owner, "hardlink")
                    for name in group
                    if name != owner
                )
            tree_factory(
                directories,
                tuple(sorted(planned_files, key=lambda item: item.path)),
                tuple(sorted(planned_aliases, key=lambda item: item.path)),
                case_sensitive,
            )
            del planned_files, planned_aliases
        workspace = (
            tempfile.TemporaryDirectory(prefix=".sdk-capture-", dir=store.root)
            if retain_payloads
            else nullcontext(None)
        )
        with workspace as temp:
            scratch = Path(temp) / "bytes" if retain_payloads else None
            for group in groups.values():
                owner = min(group)
                value = before[owner]
                if any(
                    before[name].st_nlink != len(group)
                    or before[name].st_mode != value.st_mode
                    or before[name].st_size != value.st_size
                    for name in group
                ):
                    raise ValueError("SDK hard-link group is incomplete or changed")
                output_context = (
                    scratch.open("xb") if retain_payloads else nullcontext(None)
                )
                with output_context as output:
                    size, mode, digest = _stream(
                        root,
                        owner,
                        min(
                            limits.max_file_bytes,
                            max(0, limits.max_total_bytes - total),
                        ),
                        output,
                        access=access,
                    )
                if retain_payloads:
                    blob = store.put_file(scratch)
                    scratch.unlink()
                else:
                    # _stream retains its original no-follow, byte/identity and
                    # namespace checks with a hashing-only sink. No payload or
                    # temporary file is created for a freshness observation.
                    blob = BlobRef(digest, size)
                if blob.digest != digest or blob.size != size:
                    raise ValueError("SDK captured bytes changed before storage")
                total += size
                files.append(NativeSdkFile(owner, blob, bool(mode & 0o111)))
                aliases.extend(
                    NativeSdkAlias(name, owner, "hardlink")
                    for name in group
                    if name != owner
                )
        if total > limits.max_total_bytes:
            raise ValueError("SDK capture exceeds total byte bound")
        tree = tree_factory(
            directories,
            tuple(sorted(files, key=lambda item: item.path)),
            tuple(sorted(aliases, key=lambda item: item.path)),
            case_sensitive,
        )

        if capture_memory is not None:
            capture_memory["tree"] = tree.index.metadata_memory_bound

        def unchanged():
            # Re-enumerate the root too: fixed selected roots alone miss a new
            # top-level sibling that was not present at transaction start.
            if top_names() != roots or inventory() != (names, directories):
                raise ValueError("SDK tree changed during capture")
            for name, value in before.items():
                actual = metadata(name)
                if (_stamp(actual), actual.st_nlink) != (_stamp(value), value.st_nlink):
                    raise ValueError("SDK tree member changed during capture")

        unchanged()
        for item in tree.files:
            size, mode, digest = _stream(
                root, item.path, limits.max_file_bytes, access=access
            )
            if (size, bool(mode & 0o111), digest) != (
                item.blob.size,
                item.executable,
                item.blob.digest,
            ):
                raise ValueError("SDK file changed during capture")
            if retain_payloads:
                store.verify(item.blob)
        for alias in tree.aliases:
            if (
                alias.kind == "symlink"
                and _link_bytes(root, alias.path, 4096, access).decode("utf-8")
                != alias.target
            ):
                raise ValueError("SDK symbolic target changed during capture")
        unchanged()
        revalidate_owned_output()
        return tree


def materialize_native_sdk_tree(
    tree: NativeSdkTree,
    *,
    expected_identity: ContentIdentity,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
    parent: Path,
) -> Path:
    """Return a fresh verified relocation, preserving all alias relationships."""
    if not isinstance(tree, NativeSdkTree) or tree.identity != expected_identity:
        raise ValueError("SDK tree differs from its requested exact identity")
    return _materialize_native_tree(
        tree,
        store=store,
        limits=limits,
        parent=parent,
        capture=capture_native_sdk_tree,
        equal=lambda actual: actual == tree,
    )


def materialize_composed_native_tree(
    tree: ComposedNativeTree,
    *,
    expected_identity: ContentIdentity,
    store: FileSystemCAS,
    limits: RetainedProjectLimits,
    parent: Path,
) -> Path:
    """Materialize complete geometry; this never admits an isolated unit subset."""
    if not isinstance(tree, ComposedNativeTree) or tree.identity != expected_identity:
        raise ValueError("Composed native tree differs from its requested identity")
    if not isinstance(limits, RetainedProjectLimits):
        raise TypeError("Composed relocation requires typed limits")
    if any(
        len(v.target.encode("utf-8")) > limits.max_file_bytes
        for v in tree.aliases
        if v.kind == "symlink"
    ):
        raise ValueError("Composed relocation exceeds alias byte bound")
    policy = tree.index

    def capture(root, **kwargs):
        return capture_composed_native_tree(
            root,
            **kwargs,
            max_descriptor_bytes=policy.max_descriptor_bytes,
            max_descriptor_total_bytes=policy.max_descriptor_total_bytes,
            max_index_bytes=policy.max_index_bytes,
            max_metadata_memory_bytes=policy.max_metadata_memory_bytes,
        )

    def equal(actual):
        # Descriptor boundaries are not physical topology. The original exact
        # index stays bound by expected_identity; independently recaptured bytes,
        # modes, aliases and directory geometry must match its complete projection.
        return (
            actual.directories == tree.directories
            and actual.files == tree.files
            and actual.aliases == tree.aliases
            and actual.case_sensitive == tree.case_sensitive
        )

    return _materialize_native_tree(
        tree,
        store=store,
        limits=limits,
        parent=parent,
        capture=capture,
        equal=equal,
    )


def _materialize_native_tree(tree, *, store, limits, parent, capture, equal):
    if not isinstance(limits, RetainedProjectLimits):
        raise TypeError("SDK relocation requires typed limits")
    if (
        len(tree.files) + len(tree.directories) + len(tree.aliases) > limits.max_entries
        or any(item.blob.size > limits.max_file_bytes for item in tree.files)
        or sum(item.blob.size for item in tree.files)
        + sum(
            len(item.target.encode("utf-8"))
            for item in tree.aliases
            if item.kind == "symlink"
        )
        > limits.max_total_bytes
    ):
        raise ValueError("SDK relocation exceeds declared bounds")
    parent = _root(Path(parent))
    for item in tree.files:
        store.verify(item.blob)
    staging = Path(tempfile.mkdtemp(prefix=".native-sdk-tree-", dir=parent))
    try:
        if tree.case_sensitive:
            _require_case_sensitive(staging)
        for name in tree.directories:
            (staging / name).mkdir()
        for item in tree.files:
            destination = staging / item.path
            require_safe_directory(destination.parent)
            store.copy_to(item.blob, destination)
            destination.chmod(0o755 if item.executable else 0o644)
        for alias in tree.aliases:
            if alias.kind == "hardlink":
                os.link(
                    staging / alias.target, staging / alias.path, follow_symlinks=False
                )
        for alias in tree.aliases:
            if alias.kind == "symlink":
                os.symlink(alias.target, staging / alias.path)
        actual = capture(
            staging,
            store=store,
            limits=limits,
            case_sensitive=tree.case_sensitive,
        )
        if not equal(actual):
            raise ValueError("SDK relocated tree differs from its exact inventory")
        return staging
    except BaseException:
        _remove_tree(staging)
        raise
