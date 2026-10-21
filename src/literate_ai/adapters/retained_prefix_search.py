"""Scoped search through an owned private allocation, never allocation authority."""

from __future__ import annotations

import json
import os
import stat
import sys

from .builders._process import run_bounded_process
from .retained_posix_acl import DescriptorAclCustody, project_secondary_acl
from .retained_project_inputs import _DirectoryAccess

_DEFAULT = b"user::rwx\ngroup::---\nother::---\n"
_PROBE = """import json,os,sys
r=json.loads(sys.argv[1]);os.setgroups(r['groups']);os.setgid(r['gid']);os.setuid(r['uid'])
print(json.dumps([os.access(p,m) for p,m in r['paths']]))
"""


def secondary_path_access(account, paths):
    """Observe kernel permissions after a real, irreversible child identity drop."""
    request = {
        "uid": account.uid,
        "gid": account.gid,
        "groups": list(account.groups),
        "paths": [(str(path), mode) for path, mode in paths],
    }
    result = run_bounded_process(
        (sys.executable, "-I", "-S", "-c", _PROBE, json.dumps(request)),
        cwd=None,
        environment={"LANG": "C", "LC_ALL": "C"},
        timeout_seconds=20,
        stdout_limit_bytes=4096,
        stderr_limit_bytes=4096,
        error_prefix="retained.views.access",
        trace=False,
        terminate_descendants=True,
    )
    if result.returncode:
        raise ValueError("retained.views.secondary-access-probe-failed")
    values = json.loads(result.stdout)
    if (
        type(values) is not list
        or len(values) != len(paths)
        or any(type(value) is not bool for value in values)
    ):
        raise ValueError("retained.views.secondary-access-probe-invalid")
    return tuple(values)


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


class PrivatePrefixSearch:
    """Resource mechanics; the enclosing view owner binds plan and live allocation.

    Protect reachable siblings before adding named-user search. Existing private
    siblings are pinned without changing them. No outside ancestor is modified.
    """

    def __init__(self, prefix, namespace, anchors, backend, account):
        self.prefix, self.namespace = prefix, namespace
        self.anchors, self.backend, self.account = anchors, backend, account
        self._root = None
        self._children = {}
        self._active = self._closed = False
        self._cleanup_ok = None
        self._fd = anchors[0].directory(())[0]
        parent, searchable = secondary_path_access(
            account, ((prefix.parent, os.X_OK), (prefix, os.X_OK))
        )
        if not parent:
            raise ValueError("retained.views.external-ancestor-not-searchable")
        if not searchable:
            self._root = DescriptorAclCustody(backend, self._fd)

    def _inventory(self):
        result = {}
        with os.scandir(self._fd) as entries:
            for entry in entries:
                if entry.name == self.namespace.name:
                    continue
                if len(result) >= 16384:
                    raise ValueError("retained.views.prefix-entry-bound")
                info = os.stat(entry.name, dir_fd=self._fd, follow_symlinks=False)
                if info.st_uid != 0:
                    raise ValueError("retained.views.prefix-child-owner")
                result[entry.name] = _stamp(info)
        return result

    def _capture_children(self):
        for name, expected in self._inventory().items():
            # A root-owned inode with no group/other permissions stays private.
            if not stat.S_IMODE(expected[2]) & 0o077:
                self._children[name] = expected
                continue
            if not (stat.S_ISDIR(expected[2]) or stat.S_ISREG(expected[2])):
                raise ValueError("retained.views.unsupported-prefix-child")
            if (
                sum(
                    isinstance(v, DescriptorAclCustody) for v in self._children.values()
                )
                >= 64
            ):
                raise ValueError("retained.views.prefix-descriptor-bound")
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self._fd,
            )
            try:
                if (
                    _stamp(os.fstat(descriptor)) != expected
                    or _stamp(os.stat(name, dir_fd=self._fd, follow_symlinks=False))
                    != expected
                ):
                    raise ValueError("retained.views.prefix-child-changed")
                self._children[name] = DescriptorAclCustody(self.backend, descriptor)
            finally:
                os.close(descriptor)

    def _resources(self):
        return tuple(
            value
            for value in (self._root, *self._children.values())
            if isinstance(value, DescriptorAclCustody)
        )

    def _refresh_owned_metadata(self, *, cleanup=False):
        # Only known ACL owners can change expected ancestor metadata. Check every
        # current name and held inode; unrelated mode/replacement drift still fails.
        changes = {}
        errors = []
        for resource in self._resources():
            try:
                if not resource.closed:
                    resource.require_current()
            except BaseException as error:
                errors.append(error)
            expected = (
                resource.original_metadata
                if resource.closed
                else resource.expected_metadata
            )
            changes[expected[:2]] = expected[:5]
        if not changes:
            return
        for anchor_index, anchor in enumerate(self.anchors):
            replacement = []
            try:
                for index, (name, descriptor, original) in enumerate(anchor._absolute):
                    expected = changes.get(original[:2], original)
                    if cleanup and anchor_index and original[:2] not in changes:
                        # Leave unrelated backing-root drift refused. Teardown
                        # only refreshes owned permissions; the prefix anchor
                        # still verifies the complete ingress used for unmount.
                        replacement.append((name, descriptor, original))
                        continue
                    current = os.stat(
                        name,
                        **({"dir_fd": anchor._absolute[index - 1][1]} if index else {}),
                        follow_symlinks=False,
                    )
                    if _DirectoryAccess._identity(current) != expected or (
                        _DirectoryAccess._identity(os.fstat(descriptor)) != expected
                    ):
                        raise ValueError(
                            "retained.views.owned-ancestor-transition-drift"
                        )
                    replacement.append((name, descriptor, expected))
                anchor._absolute[:] = replacement
            except BaseException as error:
                errors.append(error)
        if errors:
            raise ValueError(
                "retained.views.owned-ancestor-transition-refused"
            ) from errors[0]

    def require_current(self):
        if self._closed:
            raise ValueError("retained.views.prefix-scope-closed")
        if not secondary_path_access(self.account, ((self.prefix.parent, os.X_OK),))[0]:
            raise ValueError("retained.views.external-ancestor-not-searchable")
        if self._root is not None:
            self._root.require_current()
        if self._active:
            inventory = self._inventory()
            if not set(self._children) <= set(inventory) or any(
                stat.S_IMODE(inventory[name][2]) & 0o077
                for name in inventory.keys() - self._children.keys()
            ):
                raise ValueError("retained.views.prefix-inventory-changed")
            for name, value in self._children.items():
                if isinstance(value, DescriptorAclCustody):
                    value.require_current()
                    expected = value.expected_metadata[:5]
                else:
                    expected = value
                if inventory[name] != expected:
                    raise ValueError("retained.views.prefix-child-changed")

    def activate(self):
        self.require_current()
        if self._root is None:
            return
        try:
            self._capture_children()
            self._active = True
            uid = self.account.uid
            for resource in self._resources()[1:]:
                resource.replace(
                    project_secondary_acl(resource.original[2], uid, "---"),
                    resource.original.get(3),
                )
            self._root.replace(
                project_secondary_acl(self._root.original[2], uid, "--x"),
                project_secondary_acl(self._root.original[3] or _DEFAULT, uid, "---"),
            )
            self._refresh_owned_metadata()
            self.require_current()
        except BaseException:
            self.close()
            raise

    def revoke(self):
        if self._root is None or not self._active:
            return
        self._active = False
        self._root.replace(self._root.original[2], self._root.original[3])
        errors = []
        for resource in self._resources()[1:]:
            try:
                if not resource.closed:
                    resource.restore()
            except BaseException as error:
                errors.append(error)
        try:
            self._refresh_owned_metadata(cleanup=True)
        except BaseException as error:
            errors.append(error)
        if errors:
            raise ValueError("retained.views.prefix-revoke-incomplete") from errors[0]
        self._children.clear()

    def close(self):
        if self._closed:
            if self._cleanup_ok is False:
                raise ValueError("retained.views.prefix-cleanup-incomplete")
            return
        errors = []
        # Revoke ingress before restoring potentially readable sibling objects.
        for resource in self._resources():
            try:
                if not resource.closed:
                    resource.restore()
            except BaseException as error:
                errors.append(error)
        try:
            self._refresh_owned_metadata(cleanup=True)
        except BaseException as error:
            errors.append(error)
        self._closed = True
        self._cleanup_ok = not errors
        if errors:
            raise ValueError("retained.views.prefix-cleanup-incomplete") from errors[0]
