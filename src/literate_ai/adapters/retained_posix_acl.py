"""FreeBSD POSIX ACL custody on held descriptors, not permission authority.

The workspace owner must separately prove allocation, reviewed scope, account
currentness and current names/ancestors. This adapter neither opens paths nor
turns deserialized configuration into authority.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
import tempfile
from pathlib import Path

_ACCESS = 2
_DEFAULT = 3
_TEXT_LIMIT = 65536


class FreeBsdPosixAcl:
    def __init__(self):
        if not sys.platform.startswith("freebsd"):
            raise ValueError("retained.acl.unsupported-platform")
        self.libc = ctypes.CDLL(None, use_errno=True)
        pointer = ctypes.c_void_p
        definitions = {
            "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], pointer),
            "acl_set_fd_np": ([ctypes.c_int, pointer, ctypes.c_int], ctypes.c_int),
            "acl_delete_fd_np": ([ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "acl_from_text": ([ctypes.c_char_p], pointer),
            "acl_to_text_np": (
                [pointer, ctypes.POINTER(ctypes.c_ssize_t), ctypes.c_int],
                pointer,
            ),
            "acl_get_brand_np": ([pointer, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
            "acl_valid": ([pointer], ctypes.c_int),
            "acl_free": ([pointer], ctypes.c_int),
        }
        try:
            for name, (arguments, result) in definitions.items():
                function = getattr(self.libc, name)
                function.argtypes = arguments
                function.restype = result
        except AttributeError as exc:
            raise ValueError("retained.acl.backend-unavailable") from exc

    def _check(self, result):
        if result != 0:
            code = ctypes.get_errno()
            raise OSError(code, "retained.acl.native-operation-refused")

    def read(self, fd, kind):
        if kind not in (_ACCESS, _DEFAULT):
            raise ValueError("retained.acl.unsupported-kind")
        pointer = self.libc.acl_get_fd_np(fd, kind)
        if not pointer:
            self._check(-1)
        text = None
        try:
            brand = ctypes.c_int()
            self._check(self.libc.acl_get_brand_np(pointer, ctypes.byref(brand)))
            if brand.value != 1:
                raise ValueError("retained.acl.posix-brand-required")
            size = ctypes.c_ssize_t()
            text = self.libc.acl_to_text_np(pointer, ctypes.byref(size), 2)
            if not text:
                self._check(-1)
            if not 0 <= size.value <= _TEXT_LIMIT:
                raise ValueError("retained.acl.text-budget")
            return ctypes.string_at(text, size.value)
        finally:
            if text:
                self.libc.acl_free(text)
            self.libc.acl_free(pointer)

    def write(self, fd, kind, text):
        if kind not in (_ACCESS, _DEFAULT):
            raise ValueError("retained.acl.unsupported-kind")
        if type(text) is not bytes or len(text) > _TEXT_LIMIT or b"\0" in text:
            raise ValueError("retained.acl.invalid-text")
        if not text:
            if kind != _DEFAULT:
                raise ValueError("retained.acl.access-required")
            self._check(self.libc.acl_delete_fd_np(fd, kind))
            return
        pointer = self.libc.acl_from_text(text)
        if not pointer:
            self._check(-1)
        try:
            self._check(self.libc.acl_valid(pointer))
            self._check(self.libc.acl_set_fd_np(fd, pointer, kind))
        finally:
            self.libc.acl_free(pointer)


class DescriptorAclCustody:
    """One owned inode's original ACLs, restored even after observed drift.

    The caller owns path/ancestor custody and descriptor budgets. Restoration
    operates on this held inode even if its pathname has subsequently moved.
    """

    def __init__(self, backend, fd):
        if type(fd) is not int or fd < 0:
            raise ValueError("retained.acl.descriptor-required")
        self.backend = backend
        self.fd = os.dup(fd)
        self.closed = False
        try:
            info = os.fstat(self.fd)
            if (
                os.geteuid() != 0
                or info.st_uid != 0
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
            ):
                raise ValueError("retained.acl.exclusive-root-owned-resource-required")
            self.directory = stat.S_ISDIR(info.st_mode)
            self.original = self._read_all()
            self.expected = dict(self.original)
            self.original_metadata = self._metadata()
            self.expected_metadata = self.original_metadata
        except BaseException:
            os.close(self.fd)
            self.closed = True
            raise

    def _metadata(self):
        info = os.fstat(self.fd)
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink if not self.directory else None,
        )

    def _read_all(self):
        kinds = (_ACCESS, _DEFAULT) if self.directory else (_ACCESS,)
        return {kind: self.backend.read(self.fd, kind) for kind in kinds}

    def require_current(self):
        if self.closed:
            raise ValueError("retained.acl.custody-closed")
        if (
            self._metadata() != self.expected_metadata
            or self._read_all() != self.expected
        ):
            raise ValueError("retained.acl.permissions-changed")

    def replace(self, access, default=None):
        self.require_current()
        if self.directory != (default is not None):
            raise ValueError("retained.acl.exact-access-and-default-required")
        # Each completed write becomes current immediately. A following failed
        # write remains recoverable by restore(), never a successful grant.
        self.backend.write(self.fd, _ACCESS, access)
        self.expected[_ACCESS] = self.backend.read(self.fd, _ACCESS)
        self.expected_metadata = self._metadata()
        if self.directory:
            self.backend.write(self.fd, _DEFAULT, default)
            self.expected[_DEFAULT] = self.backend.read(self.fd, _DEFAULT)
            self.expected_metadata = self._metadata()
        self.require_current()

    def _holds_original_inode(self):
        try:
            actual = os.fstat(self.fd)
        except OSError:
            return False
        original = self.original_metadata
        return (actual.st_dev, actual.st_ino, stat.S_IFMT(actual.st_mode)) == (
            original[0],
            original[1],
            stat.S_IFMT(original[2]),
        )

    def restore(self):
        if self.closed:
            raise ValueError("retained.acl.custody-closed")
        drift = None
        try:
            try:
                self.require_current()
            except (ValueError, OSError) as exc:
                drift = exc
            errors = []
            for kind, text in self.original.items():
                try:
                    if not self._holds_original_inode():
                        raise ValueError("retained.acl.held-inode-changed")
                    self.backend.write(self.fd, kind, text)
                except (ValueError, OSError) as exc:
                    errors.append(exc)
            if (
                errors
                or self._metadata() != self.original_metadata
                or self._read_all() != self.original
            ):
                raise ValueError("retained.acl.cleanup-incomplete")
            if drift is not None:
                raise ValueError("retained.acl.drift-restored") from drift
        finally:
            # A same-process descriptor-reuse bug must not close or mutate a
            # different inode. The original descriptor is already gone then.
            if self._holds_original_inode():
                os.close(self.fd)
            self.closed = True

    def __enter__(self):
        try:
            self.require_current()
        except BaseException:
            self.restore()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.restore()
        return False


def preflight_freebsd_posix_acl(parent: Path, uid: int) -> None:
    """Prove reversible access/default ACL support before retained input capture."""

    backend = FreeBsdPosixAcl()
    path = Path(tempfile.mkdtemp(prefix=".litai-acl-preflight-", dir=parent))
    descriptor = None
    custody = None
    try:
        path.chmod(0o700)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            custody = DescriptorAclCustody(backend, descriptor)
            default_base = custody.original[_DEFAULT] or custody.original[_ACCESS]
            custody.replace(
                project_secondary_acl(custody.original[_ACCESS], uid, "--x"),
                project_secondary_acl(default_base, uid, "---"),
            )
            custody.require_current()
            custody.restore()
        except OSError as error:
            if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise ValueError(
                    "retained.acl.filesystem-unsupported: enable POSIX.1e ACLs "
                    "on the retained execution workspace filesystem"
                ) from error
            raise
    finally:
        if custody is not None and not custody.closed:
            custody.restore()
        if descriptor is not None:
            os.close(descriptor)
        try:
            path.rmdir()
        except OSError as error:
            raise ValueError("retained.acl.preflight-cleanup-incomplete") from error


def project_secondary_acl(original, uid, permissions):
    """Change one named UID without expanding another principal's access.

    POSIX mask widening would otherwise unmask permissions on existing named
    users/groups. Clamp those entries to their prior effective permissions first.
    This pure projection is not reviewed scope or allocation authority.
    """
    import re

    if type(uid) is not int or not 1 <= uid < 2**32 - 1:
        raise ValueError("retained.acl.nonroot-uid-required")
    if type(original) is not bytes or not 0 < len(original) <= _TEXT_LIMIT:
        raise ValueError("retained.acl.invalid-original")

    def bits(value):
        if type(value) is not str or re.fullmatch(r"[r-][w-][x-]", value) is None:
            raise ValueError("retained.acl.invalid-permissions")
        return sum(
            bit for char, bit in zip(value, (4, 2, 1), strict=True) if char != "-"
        )

    def chars(value):
        return "".join(
            char if value & bit else "-"
            for char, bit in zip("rwx", (4, 2, 1), strict=True)
        )

    requested = bits(permissions)
    entries = {}
    for line in original.decode("ascii").splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        fields = line.split(":")
        if len(fields) != 3:
            raise ValueError("retained.acl.invalid-entry")
        tag, qualifier, permission = fields
        if tag not in ("user", "group", "mask", "other") or (
            tag in ("mask", "other") and qualifier
        ):
            raise ValueError("retained.acl.invalid-tag")
        if qualifier:
            if (
                not qualifier.isdecimal()
                or str(int(qualifier)) != qualifier
                or not 0 <= int(qualifier) < 2**32 - 1
            ):
                raise ValueError("retained.acl.invalid-qualifier")
        key = (tag, qualifier)
        if key in entries:
            raise ValueError("retained.acl.duplicate-entry")
        entries[key] = bits(permission)
    if not {("user", ""), ("group", ""), ("other", "")} <= set(entries):
        raise ValueError("retained.acl.incomplete-original")
    named = any(qualifier for tag, qualifier in entries)
    if named and ("mask", "") not in entries:
        raise ValueError("retained.acl.original-mask-required")
    mask = entries.get(("mask", ""), entries[("group", "")])
    for key in entries:
        if key[0] == "group" or (key[0] == "user" and key[1]):
            entries[key] &= mask
    entries[("user", str(uid))] = requested
    entries[("mask", "")] = mask | requested
    ordered = (
        [("user", "")]
        + sorted(
            (key for key in entries if key[0] == "user" and key[1]),
            key=lambda key: int(key[1]),
        )
        + [("group", "")]
        + sorted(
            (key for key in entries if key[0] == "group" and key[1]),
            key=lambda key: int(key[1]),
        )
        + [("mask", ""), ("other", "")]
    )
    result = "".join(
        tag + ":" + qualifier + ":" + chars(entries[(tag, qualifier)]) + "\n"
        for tag, qualifier in ordered
    ).encode("ascii")
    if len(result) > _TEXT_LIMIT:
        raise ValueError("retained.acl.text-budget")
    return result
