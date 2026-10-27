"""POSIX mask projection and actual descriptor custody on an enabled FreeBSD guest."""

import errno
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters import retained_posix_acl as acl
from literate_ai.adapters.retained_posix_acl import (
    DescriptorAclCustody,
    FreeBsdPosixAcl,
    project_secondary_acl,
)

BASE = b"user::rwx\ngroup::---\nother::---\n"


def permissions(bits):
    return "".join(
        char if bits & bit else "-" for char, bit in zip("rwx", (4, 2, 1), strict=True)
    )


def entries(text):
    return dict(
        (tuple(line.split(":")[:2]), line.split(":")[2])
        for line in text.decode("ascii").splitlines()
    )


class PosixAclProjectionTests(unittest.TestCase):
    def test_mask_widening_preserves_other_effective_permissions_exhaustively(self):
        for mask in range(8):
            for existing in range(8):
                for granted in range(8):
                    original = (
                        f"user::rwx\nuser:977:{permissions(existing)}\n"
                        f"group::{permissions(existing ^ 7)}\n"
                        f"group:978:{permissions(existing)}\n"
                        f"mask::{permissions(mask)}\nother::r--\n"
                    ).encode("ascii")
                    projected = entries(
                        project_secondary_acl(original, 2001, permissions(granted))
                    )
                    self.assertEqual(
                        projected["user", "977"], permissions(existing & mask)
                    )
                    self.assertEqual(
                        projected["group", ""], permissions((existing ^ 7) & mask)
                    )
                    self.assertEqual(
                        projected["group", "978"], permissions(existing & mask)
                    )
                    self.assertEqual(projected["user", "2001"], permissions(granted))
                    self.assertEqual(projected["mask", ""], permissions(mask | granted))
                    self.assertEqual(projected["user", ""], "rwx")
                    self.assertEqual(projected["other", ""], "r--")

    def test_exact_deny_overrides_existing_named_grant(self):
        original = b"user::rwx\nuser:2001:rwx\ngroup::r--\nmask::rwx\nother::r--\n"
        self.assertEqual(
            entries(project_secondary_acl(original, 2001, "---"))["user", "2001"], "---"
        )

    def test_rejects_malformed_and_ambiguous_originals(self):
        for original in (
            b"",
            BASE + b"other::rwx\n",
            b"user::rwx\nother::---\n",
            b"user::rwx\nuser:977:rwx\ngroup::---\nother::---\n",
            BASE + b"user:00977:r--\n",
            BASE + b"user:root:rwx\n",
            BASE + b"everyone::rwx\n",
            BASE + b"mask:977:rwx\n",
            b"x" * 65537,
            b"user::rwz\ngroup::---\nother::---\n",
        ):
            with self.subTest(original=original[:80]), self.assertRaises(ValueError):
                project_secondary_acl(original, 2001, "r--")

    def test_rejects_root_boolean_and_unbounded_uids(self):
        for uid in (0, True, -1, 2**32 - 1, "2001"):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                project_secondary_acl(BASE, uid, "r--")

    def test_unsupported_platform_refuses_before_native_calls(self):
        if sys.platform.startswith("freebsd"):
            self.skipTest("native backend supported on this platform")
        with self.assertRaisesRegex(ValueError, "unsupported-platform"):
            FreeBsdPosixAcl()

    def test_non_posix_brands_refuse_before_conversion_and_free_native_handle(self):
        for brand in (0, 2, 3):
            with self.subTest(brand=brand):
                backend = FreeBsdPosixAcl.__new__(FreeBsdPosixAcl)
                backend.libc = Mock()
                handle = object()
                backend.libc.acl_get_fd_np.return_value = handle

                def observe_brand(_handle, output, value=brand):
                    acl.ctypes.cast(
                        output, acl.ctypes.POINTER(acl.ctypes.c_int)
                    ).contents.value = value
                    return 0

                backend.libc.acl_get_brand_np.side_effect = observe_brand
                with self.assertRaisesRegex(ValueError, "posix-brand-required"):
                    backend.read(17, 2)
                backend.libc.acl_to_text_np.assert_not_called()
                backend.libc.acl_set_fd_np.assert_not_called()
                backend.libc.acl_free.assert_called_once_with(handle)

    def test_preflight_applies_reads_restores_and_removes_probe(self):
        observed = {}

        class Custody:
            def __init__(self, backend, descriptor):
                observed["backend"] = backend
                observed["directory"] = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                self.original = {2: BASE, 3: BASE}
                self.closed = False

            def replace(self, access, default):
                observed["access"] = access
                observed["default"] = default

            def require_current(self):
                observed["current"] = True

            def restore(self):
                observed["restored"] = True
                self.closed = True

        backend = object()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(acl, "FreeBsdPosixAcl", return_value=backend),
            patch.object(acl, "DescriptorAclCustody", Custody),
        ):
            parent = Path(directory)
            acl.preflight_freebsd_posix_acl(parent, 2001)
            self.assertEqual(list(parent.iterdir()), [])
        self.assertIs(observed["backend"], backend)
        self.assertTrue(observed["directory"])
        self.assertEqual(entries(observed["access"])["user", "2001"], "--x")
        self.assertEqual(entries(observed["default"])["user", "2001"], "---")
        self.assertTrue(observed["current"])
        self.assertTrue(observed["restored"])

    def test_preflight_builds_temporary_default_from_access_when_absent(self):
        observed = {}

        class Custody:
            def __init__(self, _backend, _descriptor):
                self.original = {2: BASE, 3: b""}
                self.closed = False

            def replace(self, _access, default):
                observed["default"] = default

            def require_current(self):
                pass

            def restore(self):
                self.closed = True

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(acl, "FreeBsdPosixAcl", return_value=object()),
            patch.object(acl, "DescriptorAclCustody", Custody),
        ):
            acl.preflight_freebsd_posix_acl(Path(directory), 2001)
        self.assertEqual(entries(observed["default"])["user", "2001"], "---")

    def test_preflight_names_unsupported_filesystem_and_removes_probe(self):
        def unsupported(_backend, _descriptor):
            raise OSError(errno.EOPNOTSUPP, "ACLs disabled")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(acl, "FreeBsdPosixAcl", return_value=object()),
            patch.object(acl, "DescriptorAclCustody", side_effect=unsupported),
        ):
            parent = Path(directory)
            with self.assertRaisesRegex(
                ValueError, "filesystem-unsupported: enable POSIX.1e ACLs"
            ):
                acl.preflight_freebsd_posix_acl(parent, 2001)
            self.assertEqual(list(parent.iterdir()), [])

    def test_preflight_open_failure_removes_probe(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(acl, "FreeBsdPosixAcl", return_value=object()),
            patch.object(acl.os, "open", side_effect=PermissionError("denied")),
        ):
            parent = Path(directory)
            with self.assertRaisesRegex(PermissionError, "denied"):
                acl.preflight_freebsd_posix_acl(parent, 2001)
            self.assertEqual(list(parent.iterdir()), [])


@unittest.skipUnless(
    sys.platform.startswith("freebsd")
    and os.environ.get("LITAI_FREEBSD_POSIX_ACL_DISABLED_ROOT")
    and os.environ.get("LITAI_FREEBSD_POSIX_ACL_ENABLED_ROOT"),
    "requires explicit FreeBSD UFS ACL-disabled and ACL-enabled mounts",
)
class FreeBsdPosixAclPreflightAcceptanceTests(unittest.TestCase):
    def assert_no_probe(self, root):
        self.assertEqual(list(root.glob(".litai-acl-preflight-*")), [])

    def test_disabled_ufs_refuses_before_capture_and_cleans_probe(self):
        root = Path(os.environ["LITAI_FREEBSD_POSIX_ACL_DISABLED_ROOT"])
        with self.assertRaisesRegex(
            ValueError, "filesystem-unsupported: enable POSIX.1e ACLs"
        ):
            acl.preflight_freebsd_posix_acl(root, 2001)
        self.assert_no_probe(root)

    def test_enabled_ufs_applies_restores_and_cleans_probe(self):
        root = Path(os.environ["LITAI_FREEBSD_POSIX_ACL_ENABLED_ROOT"])
        acl.preflight_freebsd_posix_acl(root, 2001)
        self.assert_no_probe(root)


@unittest.skipUnless(
    sys.platform.startswith("freebsd") and hasattr(os, "geteuid") and os.geteuid() == 0,
    "requires privileged FreeBSD POSIX ACL fixture",
)
class FreeBsdDescriptorAclTests(unittest.TestCase):
    def setUp(self):
        self.backend = FreeBsdPosixAcl()
        temporary = tempfile.TemporaryDirectory(prefix="litai-descriptor-acl-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "selected"
        self.path.write_bytes(b"selected fixture\n")
        self.path.chmod(0o600)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                self.backend.read(fd, 3)
            except OSError as exc:
                if os.environ.get("LITAI_REQUIRE_FREEBSD_POSIX_ACL") == "1":
                    raise AssertionError(
                        "required POSIX ACL filesystem is unavailable"
                    ) from exc
                self.skipTest("POSIX ACLs not enabled on fixture filesystem")
        finally:
            os.close(fd)

    def hold(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            held = DescriptorAclCustody(self.backend, fd)
        finally:
            os.close(fd)
        self.addCleanup(lambda: None if held.closed else held.restore())
        return held

    def actor(self, operation, uid=2001):
        child = os.fork()
        if child == 0:
            try:
                os.setgroups([uid])
                os.setgid(uid)
                os.setuid(uid)
                operation()
            except PermissionError:
                os._exit(12)
            except BaseException:
                os._exit(13)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        return os.waitstatus_to_exitcode(status)

    def read_as_secondary(self, path):
        return self.actor(
            lambda: os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        )

    def test_selected_access_new_child_denial_and_exact_restoration(self):
        other = self.root / "other"
        other.write_bytes(b"unrelated\n")
        other.chmod(0o644)
        directory, selected, unrelated = (
            self.hold(p) for p in (self.root, self.path, other)
        )
        self.assertEqual(self.read_as_secondary(self.path), 12)
        directory.replace(
            project_secondary_acl(directory.original[2], 2001, "--x"),
            project_secondary_acl(BASE, 2001, "---"),
        )
        selected.replace(project_secondary_acl(selected.original[2], 2001, "r--"))
        unrelated.replace(project_secondary_acl(unrelated.original[2], 2001, "---"))
        self.assertEqual(self.read_as_secondary(self.path), 0)
        self.assertEqual(self.read_as_secondary(other), 12)
        new = self.root / "new"
        new.write_bytes(b"new unrelated\n")
        new.chmod(0o644)
        self.assertEqual(self.read_as_secondary(new), 12)
        for held in (directory, selected, unrelated):
            held.require_current()
        for held in (unrelated, selected, directory):
            held.restore()
        self.assertEqual(self.read_as_secondary(self.path), 12)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_mask_projection_preserves_actual_existing_user_execution_denial(self):
        self.path.write_bytes(b"#!/bin/sh\nexit 0\n")
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.backend.write(
                fd, 2, b"user::rwx\nuser:977:rwx\ngroup::---\nmask::r--\nother::---\n"
            )
        finally:
            os.close(fd)
        directory, selected = self.hold(self.root), self.hold(self.path)
        directory.replace(
            project_secondary_acl(
                project_secondary_acl(directory.original[2], 977, "--x"), 2001, "--x"
            ),
            directory.original[3],
        )

        def execute():
            os.execve(str(self.path), [str(self.path)], {"PATH": "/bin:/usr/bin"})

        self.assertEqual(self.actor(execute, 977), 12)
        selected.replace(project_secondary_acl(selected.original[2], 2001, "r-x"))
        self.assertEqual(self.actor(execute, 2001), 0)
        self.assertEqual(self.actor(execute, 977), 12)
        selected.restore()
        directory.restore()

    def test_extended_default_acl_restores_exactly(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.backend.write(
                fd, 3, b"user::rwx\nuser:977:rwx\ngroup::---\nmask::r--\nother::---\n"
            )
        finally:
            os.close(fd)
        held = self.hold(self.root)
        held.replace(
            project_secondary_acl(held.original[2], 2001, "--x"),
            project_secondary_acl(held.original[3], 2001, "---"),
        )
        held.restore()

    def test_currentness_drift_is_refused_and_permissions_restored(self):
        held = self.hold(self.path)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permissions-changed"):
            held.require_current()
        with self.assertRaisesRegex(ValueError, "drift-restored"):
            held.restore()
        self.assertTrue(held.closed)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_context_entry_restores_and_closes_custody(self):
        held = self.hold(self.path)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "drift-restored"), held:
            self.fail("changed custody must not enter")
        self.assertTrue(held.closed)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_command_exception_restores_and_closes_custody(self):
        held = self.hold(self.path)
        with self.assertRaisesRegex(RuntimeError, "command failed"), held:
            held.replace(project_secondary_acl(held.original[2], 2001, "r--"))
            raise RuntimeError("command failed")
        self.assertTrue(held.closed)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_path_replacement_does_not_mutate_replacement_inode(self):
        held = self.hold(self.path)
        self.path.rename(self.root / "original")
        self.path.write_bytes(b"replacement\n")
        self.path.chmod(0o600)
        held.replace(project_secondary_acl(held.original[2], 2001, "r--"))
        held.restore()
        self.assertEqual(self.path.read_bytes(), b"replacement\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_reused_descriptor_never_mutates_or_closes_the_other_inode(self):
        held = self.hold(self.path)
        other = self.root / "other"
        other.write_bytes(b"unrelated\n")
        other.chmod(0o600)
        fd = os.open(other, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            original = self.backend.read(fd, 2)
            os.dup2(fd, held.fd)
        finally:
            os.close(fd)
        try:
            with self.assertRaisesRegex(ValueError, "cleanup-incomplete"):
                held.restore()
            self.assertEqual(self.backend.read(held.fd, 2), original)
            self.assertEqual(os.fstat(held.fd).st_ino, other.stat().st_ino)
        finally:
            os.close(held.fd)

    def test_hardlink_special_and_foreign_owner_refuse_without_descriptor_leaks(self):
        os.link(self.path, self.root / "alias")
        foreign = self.root / "foreign"
        foreign.write_bytes(b"foreign\n")
        os.chown(foreign, 2001, 2001)
        before = len(os.listdir("/dev/fd"))
        for path in (self.path, Path("/dev/null"), foreign):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                with self.assertRaisesRegex(ValueError, "exclusive-root-owned"):
                    DescriptorAclCustody(self.backend, fd)
            finally:
                os.close(fd)
        self.assertEqual(len(os.listdir("/dev/fd")), before)
