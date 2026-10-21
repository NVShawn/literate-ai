"""No-follow SDK tree custody; neither capture nor relocation issues admission."""

from __future__ import annotations

import os
import stat
import tempfile
from collections import defaultdict
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
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_sdk_tree_products import (
    NativeSdkTreeBuildLayout,
    NativeSdkTreeSnapshot,
)
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias, NativeSdkTree
from literate_ai.contracts.native_sdks import NativeSdkFile
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
    if store.root.is_relative_to(root):
        raise ValueError("SDK content store must be outside the captured tree")
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
                        if len(result) >= min(limits.max_entries, 16384):
                            raise ValueError("SDK capture exceeds entry bound")
                        result.append(entry.name)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            return tuple(sorted(result))

        roots = top_names()

        def inventory():
            return _inventory(
                root,
                roots,
                min(limits.max_entries, 16384),
                allow_internal_symlinks=True,
                access=access,
            )

        def metadata(name):
            path = root / name
            if access is not None:
                return access.metadata(path)
            require_safe_directory(path.parent)
            return path.lstat()

        names, directories = inventory()
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
        with tempfile.TemporaryDirectory(
            prefix=".sdk-capture-", dir=store.root
        ) as temp:
            scratch = Path(temp) / "bytes"
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
                with scratch.open("xb") as output:
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
                blob = store.put_file(scratch)
                scratch.unlink()
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
        tree = NativeSdkTree(
            directories,
            tuple(sorted(files, key=lambda item: item.path)),
            tuple(sorted(aliases, key=lambda item: item.path)),
            case_sensitive,
        )

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
            store.verify(item.blob)
        for alias in tree.aliases:
            if (
                alias.kind == "symlink"
                and _link_bytes(root, alias.path, 4096, access).decode("utf-8")
                != alias.target
            ):
                raise ValueError("SDK symbolic target changed during capture")
        unchanged()
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
        actual = capture_native_sdk_tree(
            staging,
            store=store,
            limits=limits,
            case_sensitive=tree.case_sensitive,
        )
        if actual != tree:
            raise ValueError("SDK relocated tree differs from its exact inventory")
        return staging
    except BaseException:
        _remove_tree(staging)
        raise
