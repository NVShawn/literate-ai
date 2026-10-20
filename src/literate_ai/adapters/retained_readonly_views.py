"""Owned FreeBSD views, not command or receipt admission.

The workspace owns content/inventory verification around commands. This owner
binds paths, mount flags, account/allocation currentness and ACL restoration.
Original private snapshots never receive delegated permissions.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan

from .builders._process import run_bounded_process
from .retained_allocation import RetainedAllocationOwner
from .retained_posix_acl import (
    DescriptorAclCustody,
    FreeBsdPosixAcl,
    project_secondary_acl,
)
from .retained_project_inputs import _DirectoryAccess
from .retained_test_identity import require_current_secondary_test_identity
from .user_paths import resolve_freebsd_view_system_paths

_DEFAULT = b"user::rwx\ngroup::---\nother::---\n"


def _node(metadata):
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
        raise ValueError("retained.views.root-owned-directory-required")
    return metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid


def _anchor(path, prefix, *, private):
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not path.is_relative_to(prefix)
        or len(path.parts) > 32
        or (private and path == prefix)
        or path.resolve(strict=True) != path
    ):
        raise ValueError("retained.views.allocated-path-required")
    handle = _DirectoryAccess(path, 64)
    try:
        for _, fd, _ in handle._absolute:
            metadata = os.fstat(fd)
            _node(metadata)
            if (
                stat.S_IMODE(metadata.st_mode) & 0o022
                and not metadata.st_mode & stat.S_ISVTX
            ):
                raise ValueError("retained.views.writable-ancestor")
        if private and stat.S_IMODE(os.fstat(handle.directory(())[0]).st_mode) & 0o077:
            raise ValueError("retained.views.private-backing-root-required")
        return handle
    except BaseException:
        handle.close()
        raise


def _command(argv):
    result = run_bounded_process(
        tuple(map(str, argv)),
        cwd=None,
        environment={"LANG": "C", "LC_ALL": "C"},
        timeout_seconds=20,
        stdout_limit_bytes=65536,
        stderr_limit_bytes=65536,
        error_prefix="retained.views",
        trace=False,
        terminate_descendants=True,
    )
    if result.returncode:
        raise ValueError("retained.views.mount-operation-failed")


class FreeBsdRetainedViewOwner:
    """Reserve paths before build; delegate only complete reviewed test views.

    Input mounts start private. Export mounts bind after their manifest exists.
    `activate`/`deactivate` delimit delegated access. `close` revokes traversal
    before unmounting, retains private diagnostic directories and closes handles.
    Privileged tests are trusted code, not contained by an OS sandbox here.
    """

    def __init__(self, plan, allocation, *, input_root, output_root, input_manifest):
        if not sys.platform.startswith("freebsd") or os.geteuid() != 0:
            raise ValueError("retained.views.freebsd-root-required")
        if (
            not isinstance(plan, RetainedProjectPlan)
            or plan.profile.secondary_access is None
        ):
            raise TypeError("retained.views.reviewed-policy-required")
        if not isinstance(allocation, RetainedAllocationOwner):
            raise TypeError("retained.views.live-allocation-owner-required")
        if (
            not isinstance(input_manifest, RetainedProjectManifest)
            or input_manifest.identity != plan.manifest_identity
        ):
            raise ValueError("retained.views.input-manifest-mismatch")
        if (
            not isinstance(input_root, Path)
            or not isinstance(output_root, Path)
            or input_root.is_relative_to(output_root)
            or output_root.is_relative_to(input_root)
        ):
            raise ValueError("retained.views.disjoint-backing-roots-required")
        self.plan, self.allocation = plan, allocation
        self.policy = plan.profile.secondary_access
        self._anchors, self._fixtures, self._mounts = [], {}, {}
        self._root_acl = None
        self._closed = self._failed = False
        self._cleanup_ok = None
        self._active = None
        self._events, self._paths = [], {}
        self._output_manifest = None
        self._root = None
        try:
            self._allocation_observation = allocation.require_plan(
                plan, minimum_remaining_seconds=60
            )
            require_current_secondary_test_identity(self.policy.account)
            prefix = allocation.workspace_prefix
            self._prefix = _anchor(prefix, prefix, private=False)
            self._anchors.append(self._prefix)
            self._inputs = _anchor(input_root, prefix, private=True)
            self._anchors.append(self._inputs)
            self._outputs = _anchor(output_root, prefix, private=True)
            self._anchors.append(self._outputs)
            self._backend = FreeBsdPosixAcl()
            self._system = resolve_freebsd_view_system_paths()
            self._root = Path(tempfile.mkdtemp(prefix="retained-views-", dir=prefix))
            self._root_node = _node(self._root.lstat())
            self._root_acl = self._custody(self._root, None)
            for view in self.policy.views:
                path = self._root / view.name
                path.mkdir(mode=0o755)
                self._paths[view.name] = path
            for name in self.policy.fixtures:
                path = self._root / name
                path.mkdir(mode=0o700)
                self._paths[name] = path
                self._fixtures[name] = self._custody(path, self._root_acl.fd)
            for view in self.policy.views:
                if view.origin == "inputs":
                    self._mount(view, input_manifest, self._inputs)
            self.require_current()
        except BaseException:
            self._failed = True
            self.close()
            raise

    def _custody(self, path, parent):
        descriptor = os.open(
            path if parent is None else path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            **({} if parent is None else {"dir_fd": parent}),
        )
        try:
            return DescriptorAclCustody(self._backend, descriptor)
        finally:
            os.close(descriptor)

    @property
    def paths(self):
        self._require_open()
        return dict(self._paths)

    def _require_open(self):
        if self._closed or self._failed:
            raise ValueError("retained.views.owner-unavailable")

    def _root_current(self):
        self._prefix.require_current()
        if _node(self._root.lstat()) != self._root_node:
            raise ValueError("retained.views.namespace-changed")

    def _mount(self, view, manifest, anchor):
        binding = view.bind_manifest(manifest)
        parts = tuple(view.prefix.split("/"))
        if len(parts) > 32:
            raise ValueError("retained.views.source-depth-exceeded")
        fd, lineage = anchor.directory(parts)
        target = self._paths[view.name]
        self._root_current()
        record = {
            "view": view,
            "anchor": anchor,
            "parts": parts,
            "lineage": lineage,
            "source_node": _node(os.fstat(fd)),
            "target": target,
            "before": _node(target.lstat()),
            "baseline": target.lstat().st_dev,
            "mounted": None,
        }
        # A failing command may already have mounted. Retain the cleanup target
        # before invocation, while the complete namespace is still private.
        self._mounts[view.name] = record
        _command(
            (
                self._system.mount,
                "-t",
                "nullfs",
                "-o",
                "ro,nosuid",
                anchor.root / view.prefix,
                target,
            )
        )
        self._root_current()
        # FreeBSD statvfs().f_fsid is zero even for distinct nullfs mounts.
        # The vnode device/inode identity distinguishes our mounted root instead.
        record["mounted"] = _node(target.lstat())
        if target.lstat().st_dev == record["baseline"]:
            raise ValueError("retained.views.mount-not-observed")
        self._mount_current(record)
        self._record(
            {
                "event": "mounted",
                "view_identity": canonical_identity(view.to_dict()).uri,
                "manifest_binding": binding.uri,
                "readonly": True,
                "nosuid": True,
            }
        )

    def _mount_current(self, record):
        fd, lineage = record["anchor"].directory(record["parts"])
        current = os.statvfs(record["target"])
        if (
            lineage != record["lineage"]
            or _node(os.fstat(fd)) != record["source_node"]
            or _node(record["target"].lstat()) != record["mounted"]
            or not current.f_flag & os.ST_RDONLY
            or not current.f_flag & os.ST_NOSUID
        ):
            raise ValueError("retained.views.mount-or-source-changed")

    def bind_outputs(self, manifest):
        self.require_current()
        if self._output_manifest is not None:
            raise ValueError("retained.views.outputs-already-bound")
        try:
            for view in self.policy.views:
                if view.origin == "exports":
                    self._mount(view, manifest, self._outputs)
            self._output_manifest = manifest.identity
            self.require_current()
        except BaseException:
            self._failed = True
            self.close()
            raise

    def require_current(self, *, minimum_remaining_seconds=30):
        self._require_open()
        try:
            self._allocation_observation = self.allocation.require_plan(
                self.plan, minimum_remaining_seconds=minimum_remaining_seconds
            )
            require_current_secondary_test_identity(self.policy.account)
            self._root_current()
            self._root_acl.require_current()
            for name, resource in self._fixtures.items():
                if _node(self._paths[name].lstat()) != _node(os.fstat(resource.fd)):
                    raise ValueError("retained.views.fixture-changed")
                resource.require_current()
            for record in self._mounts.values():
                self._mount_current(record)
        except BaseException:
            self._failed = True
            self.close()
            raise

    def _fixtures_empty(self):
        for name in self._fixtures:
            with os.scandir(self._paths[name]) as children:
                if next(children, None) is not None:
                    raise ValueError("retained.views.fixture-not-empty")

    def activate(self, action_id):
        self._require_open()
        if (
            self._active is not None
            or action_id not in self.policy.action_ids
            or len(self._mounts) != len(self.policy.views)
        ):
            raise ValueError("retained.views.complete-eligible-test-required")
        action = next(a for a in self.plan.profile.actions if a.action_id == action_id)
        self.require_current(minimum_remaining_seconds=action.timeout_seconds + 30)
        try:
            self._fixtures_empty()
            uid = self.policy.account.uid
            for resource in self._fixtures.values():
                resource.replace(
                    project_secondary_acl(resource.original[2], uid, "rwx"),
                    project_secondary_acl(_DEFAULT, uid, "rwx"),
                )
            self._root_acl.replace(
                project_secondary_acl(self._root_acl.original[2], uid, "--x"),
                project_secondary_acl(_DEFAULT, uid, "---"),
            )
            self._active = action_id
            self.require_current(minimum_remaining_seconds=action.timeout_seconds + 30)
            self._record(
                {
                    "event": "activated",
                    "action_id": action_id,
                    "account_identity": self.policy.account.identity.uri,
                }
            )
        except BaseException:
            self._failed = True
            self.close()
            raise

    def deactivate(self):
        self._require_open()
        if self._active is None:
            raise ValueError("retained.views.no-active-action")
        try:
            self.require_current()
            uid = self.policy.account.uid
            self._root_acl.replace(
                project_secondary_acl(self._root_acl.expected[2], uid, "---"),
                self._root_acl.expected[3],
            )
            for resource in self._fixtures.values():
                resource.replace(
                    project_secondary_acl(resource.expected[2], uid, "---"),
                    project_secondary_acl(_DEFAULT, uid, "---"),
                )
            self._fixtures_empty()
            self._record({"event": "deactivated", "action_id": self._active})
            self._active = None
        except BaseException:
            self._failed = True
            self.close()
            raise

    def close(self):
        if self._closed:
            if self._cleanup_ok is False:
                raise ValueError("retained.views.cleanup-incomplete")
            return
        errors = []
        if self._root_acl is not None:
            try:
                self._root_acl.restore()
            except BaseException as exc:
                errors.append(exc)
        try:
            self._fixtures_empty()
        except BaseException as exc:
            errors.append(exc)
        for record in reversed(tuple(self._mounts.values())):
            try:
                self._root_current()
                target = record["target"]
                if target.lstat().st_dev == record["baseline"]:
                    if _node(target.lstat()) != record["before"]:
                        raise ValueError("retained.views.cleanup-target-changed")
                    continue
                if (
                    record["mounted"] is not None
                    and _node(target.lstat()) != record["mounted"]
                ):
                    raise ValueError("retained.views.cleanup-mount-changed")
                _command((self._system.unmount, target))
                if (
                    target.lstat().st_dev != record["baseline"]
                    or _node(target.lstat()) != record["before"]
                ):
                    raise ValueError("retained.views.unmount-incomplete")
            except BaseException as exc:
                errors.append(exc)
        for resource in self._fixtures.values():
            try:
                resource.restore()
            except BaseException as exc:
                errors.append(exc)
        for anchor in reversed(self._anchors):
            try:
                anchor.close()
            except BaseException as exc:
                errors.append(exc)
        self._closed = True
        self._cleanup_ok = not errors
        self._record({"event": "closed", "cleanup_ok": not errors})
        if errors:
            self._failed = True
            raise ValueError("retained.views.cleanup-incomplete") from errors[0]

    def _record(self, event):
        observation = getattr(self, "_allocation_observation", None)
        self._events.append(
            {
                "schema": "literate-ai/retained-readonly-view-observation@1",
                "plan_identity": self.plan.identity.uri,
                "policy_identity": self.policy.identity.uri,
                "allocation_observation_identity": observation.uri
                if observation
                else None,
                **event,
            }
        )

    def evidence(self):
        return tuple(dict(event) for event in self._events)

    def require_identity_current(self):
        """Recheck the reviewed account even after delegation has been closed."""
        return require_current_secondary_test_identity(self.policy.account)

    def __enter__(self):
        self.require_current()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
