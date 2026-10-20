"""Real native mounts/ACLs, with an explicit protocol-peer allocation fixture.

These tests do not qualify an actual provisioning implementation or installed
lifecycle. Native runs require an existing nonroot test account and POSIX ACLs.
"""

import errno
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_readonly_views as views
from literate_ai.adapters.retained_allocation import (
    RESPONSE_SCHEMA,
    RetainedAllocationOwner,
)
from literate_ai.adapters.worker_provisioning import configure
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
)
from literate_ai.contracts.retained_secondary_access import (
    RetainedReadOnlyView,
    RetainedSecondaryAccessPolicy,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity
from literate_ai.contracts.worker_provisioning import WorkerProvisioner
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture

_NATIVE = sys.platform.startswith("freebsd") and os.geteuid() == 0
_REQUIRED = os.environ.get("LITAI_REQUIRE_FREEBSD_READONLY_VIEWS") == "1"


class ReadOnlyViewPlatformTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX symbolic links")
    def test_lexical_escape_symlink_or_equal_private_prefix_refuses_before_open(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            prefix, outside = Path(first).resolve(), Path(second).resolve()
            link = prefix / "redirect"
            link.symlink_to(outside, target_is_directory=True)
            for path in (prefix / ".." / outside.name, link, prefix):
                with (
                    self.subTest(path=path),
                    patch.object(views, "_DirectoryAccess") as opened,
                ):
                    with self.assertRaisesRegex(ValueError, "allocated-path-required"):
                        views._anchor(path, prefix, private=True)
                    opened.assert_not_called()

    def test_unsupported_or_nonroot_refuses_before_creating_resources(self):
        for platform, uid in (("linux", 0), ("freebsd16", 2001)):
            with (
                patch.object(sys, "platform", platform),
                patch.object(os, "geteuid", return_value=uid, create=True),
                patch.object(tempfile, "mkdtemp") as create,
            ):
                with self.assertRaisesRegex(ValueError, "freebsd-root-required"):
                    views.FreeBsdRetainedViewOwner(
                        None,
                        None,
                        input_root=None,
                        output_root=None,
                        input_manifest=None,
                    )
                create.assert_not_called()


@unittest.skipUnless(
    _NATIVE or _REQUIRED, "requires native FreeBSD root and POSIX ACLs"
)
class NativeReadOnlyViewTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_NATIVE, "required FreeBSD view capability is unavailable")
        import pwd

        name = os.environ.get("LITAI_SECONDARY_TEST_ACCOUNT", "litaitest")
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            if _REQUIRED:
                self.fail("required existing nonroot secondary account is unavailable")
            self.skipTest("existing secondary account unavailable")
        self.account = RetainedSecondaryTestIdentity(
            name,
            account.pw_uid,
            account.pw_gid,
            tuple(sorted(set(os.getgrouplist(name, account.pw_gid)))),
        )
        self.prefix = Path(tempfile.mkdtemp(prefix="litai-native-views-")).resolve()
        self.prefix.chmod(0o755)
        config_directory = tempfile.TemporaryDirectory()
        self.addCleanup(config_directory.cleanup)
        config_root = Path(config_directory.name).resolve()
        inspector = config_root / "inspect.py"
        fixture_identity = canonical_identity("native filesystem protocol fixture").uri
        inspector.write_text(
            "import json,sys,hashlib\n"
            "r=json.load(sys.stdin)\n"
            "d='sha256:'+hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
            f"print(json.dumps({{'schema':{RESPONSE_SCHEMA!r},'request_identity':d,\n"
            f"'allocation_identity':{fixture_identity!r},\n"
            f"'workspace_prefix':{str(self.prefix)!r},'remaining_seconds':3600,'disposable':True}}))\n"
        )
        config = config_root / "provider.json"
        configure(
            config,
            WorkerProvisioner(
                ("never-allocate",),
                enabled=True,
                inspection_command=(sys.executable, str(inspector)),
            ),
        )
        self.inputs, self.outputs = self.prefix / "inputs", self.prefix / "outputs"
        self.inputs.mkdir(mode=0o700)
        self.outputs.mkdir(mode=0o700)
        (self.inputs / "sdk/bin").mkdir(parents=True)
        (self.inputs / "sdk/bin/tool").write_bytes(b"SDK")
        (self.inputs / "sdk/bin/tool").chmod(0o755)
        (self.inputs / "sdk/alias").symlink_to("bin/tool")
        (self.outputs / "products").mkdir()
        (self.outputs / "products/program").write_bytes(b"BIN")
        (self.outputs / "products/program").chmod(0o755)
        self.input_manifest = RetainedProjectManifest(
            ("sdk",),
            (
                RetainedProjectMember(
                    "sdk/alias",
                    8,
                    0o120000,
                    hashlib.sha256(b"bin/tool").hexdigest(),
                    "sdk",
                    "bin/tool",
                ),
                RetainedProjectMember(
                    "sdk/bin/tool", 3, 0o755, hashlib.sha256(b"SDK").hexdigest(), "sdk"
                ),
            ),
            RetainedProjectLimits(64, 4096, 4096),
            ("sdk", "sdk/bin"),
        )
        self.output_manifest = RetainedProjectManifest(
            ("products",),
            (
                RetainedProjectMember(
                    "products/program",
                    3,
                    0o755,
                    hashlib.sha256(b"BIN").hexdigest(),
                    "retained-output",
                ),
            ),
            RetainedProjectLimits(64, 4096, 4096),
            ("products",),
        )
        plan, _ = retained_project_sbom_fixture()
        policy = RetainedSecondaryAccessPolicy(
            plan.profile.worker_identity,
            self.account,
            ("test",),
            (
                RetainedReadOnlyView("runtime", "inputs", "sdk"),
                RetainedReadOnlyView("products", "exports", "products"),
            ),
            ("work",),
        )
        profile = replace(
            plan.profile,
            actions=tuple(
                replace(a, outputs=("products",)) if a.stage == "build" else a
                for a in plan.profile.actions
            ),
            package_inventory=("products",),
            secondary_access=policy,
        )
        self.plan = replace(
            plan, profile=profile, manifest_identity=self.input_manifest.identity
        )
        self.allocation = RetainedAllocationOwner(
            config, plan=self.plan, environment=os.environ, minimum_remaining_seconds=60
        )
        self.owner = None
        self.addCleanup(self.clean)

    def clean(self):
        if self.owner is not None:
            if (
                self.owner.evidence()[-1].get("event") == "closed"
                and self.owner.evidence()[-1].get("cleanup_ok") is False
            ):
                return
            self.owner.close()
            if self.owner.evidence()[-1].get("cleanup_ok"):
                shutil.rmtree(self.prefix)
        # Failed construction/cleanup directories are retained for diagnosis.

    def create(self):
        self.owner = views.FreeBsdRetainedViewOwner(
            self.plan,
            self.allocation,
            input_root=self.inputs,
            output_root=self.outputs,
            input_manifest=self.input_manifest,
        )
        return self.owner

    def actor(self, operation):
        pid = os.fork()
        if pid == 0:
            try:
                # No held root descriptors cross into the secondary operation.
                os.closerange(3, 65536)
                os.setgroups(list(self.account.groups))
                os.setgid(self.account.gid)
                os.setuid(self.account.uid)
                operation()
                os._exit(0)
            except OSError as exc:
                os._exit(exc.errno if 0 < exc.errno < 200 else 200)
            except BaseException:
                os._exit(201)
        return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])

    def test_real_views_scope_root_write_refusal_aliases_and_exact_cleanup(self):
        before = len(os.listdir("/dev/fd"))
        owner = self.create()
        paths = owner.paths
        root = paths["runtime"].parent
        original = dict(owner._root_acl.original)
        self.assertEqual(
            self.actor(lambda: (paths["runtime"] / "alias").read_bytes()), errno.EACCES
        )
        with self.assertRaisesRegex(ValueError, "complete-eligible"):
            owner.activate("test")
        owner.bind_outputs(self.output_manifest)
        with self.assertRaisesRegex(ValueError, "complete-eligible"):
            owner.activate("build")
        owner.activate("test")
        self.assertEqual(
            self.actor(lambda: (paths["runtime"] / "alias").read_bytes()), 0
        )
        self.assertEqual(
            self.actor(lambda: (paths["products"] / "program").read_bytes()), 0
        )
        self.assertEqual(
            self.actor(lambda: (self.inputs / "sdk/bin/tool").read_bytes()),
            errno.EACCES,
        )
        with self.assertRaises(OSError) as refused:
            (paths["runtime"] / "bin/tool").write_bytes(b"changed")
        self.assertEqual(refused.exception.errno, errno.EROFS)
        self.assertIn(
            self.actor(lambda: (paths["products"] / "new").touch()),
            (errno.EROFS, errno.EACCES),
        )

        def fixture():
            child = paths["work"] / "child"
            child.write_bytes(b"fixture")
            child.unlink()

        self.assertEqual(self.actor(fixture), 0)
        (root / "unrelated").mkdir(mode=0o755)
        (root / "unrelated/file").write_bytes(b"private")
        self.assertEqual(
            self.actor(lambda: (root / "unrelated/file").read_bytes()), errno.EACCES
        )
        owner.deactivate()
        self.assertEqual(
            self.actor(lambda: (paths["runtime"] / "alias").read_bytes()), errno.EACCES
        )
        owner.close()
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assertEqual(
                {kind: owner._backend.read(fd, kind) for kind in original}, original
            )
        finally:
            os.close(fd)
        self.assertEqual((self.inputs / "sdk/bin/tool").read_bytes(), b"SDK")
        self.assertEqual(len(os.listdir("/dev/fd")), before)
        self.assertTrue(owner.evidence()[-1]["cleanup_ok"])

    def test_bad_effective_flags_refuse_even_when_mount_command_succeeds(self):
        command = views._command

        def writable(argv):
            if str(argv[0]) == str(views.resolve_freebsd_view_system_paths().mount):
                argv = (*argv[:4], "rw,nosuid", *argv[5:])
            return command(argv)

        with (
            patch.object(views, "_command", side_effect=writable),
            self.assertRaisesRegex(ValueError, "mount-or-source-changed"),
        ):
            self.create()
        for root in self.prefix.glob("retained-views-*"):
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual((root / "runtime").stat().st_dev, root.stat().st_dev)

    def test_changed_account_revokes_traversal_and_unmounts(self):
        owner = self.create()
        paths = owner.paths
        owner.bind_outputs(self.output_manifest)
        owner.activate("test")
        with (
            patch.object(
                views,
                "require_current_secondary_test_identity",
                side_effect=ValueError("account changed"),
            ),
            self.assertRaisesRegex(ValueError, "account changed"),
        ):
            owner.require_current()
        self.assertEqual(
            self.actor(lambda: (paths["runtime"] / "alias").read_bytes()), errno.EACCES
        )
        self.assertTrue(owner.evidence()[-1]["cleanup_ok"])

    def test_scope_exception_closes_all_resources(self):
        before = len(os.listdir("/dev/fd"))
        owner = self.create()
        paths = owner.paths
        with self.assertRaisesRegex(RuntimeError, "command failed"):
            with owner:
                owner.bind_outputs(self.output_manifest)
                owner.activate("test")
                raise RuntimeError("command failed")
        self.assertEqual(
            self.actor(lambda: (paths["runtime"] / "alias").read_bytes()), errno.EACCES
        )
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_backing_source_replacement_revokes_and_unmounts_original_view(self):
        owner = self.create()
        paths = owner.paths
        owner.bind_outputs(self.output_manifest)
        owner.activate("test")
        source = self.inputs / "sdk"
        original = self.inputs / "original-sdk"
        source.rename(original)
        source.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            owner.require_current()
        self.assertTrue(owner.evidence()[-1]["cleanup_ok"])
        self.assertEqual(
            self.actor(lambda: (paths["runtime"] / "alias").read_bytes()), errno.EACCES
        )
        self.assertEqual((original / "bin/tool").read_bytes(), b"SDK")

    def test_replaced_view_root_restores_held_inode_and_leaves_foreign_root_alone(self):
        owner = self.create()
        paths = owner.paths
        owner.bind_outputs(self.output_manifest)
        owner.activate("test")
        for record in reversed(tuple(owner._mounts.values())):
            views._command((owner._system.unmount, record["target"]))
        root = paths["runtime"].parent
        moved = root.with_name(root.name + "-moved")
        root.rename(moved)
        root.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "cleanup-incomplete"):
            owner.require_current()
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode), 0o700)
        self.assertEqual(
            self.actor(lambda: (moved / "runtime/alias").read_bytes()), errno.EACCES
        )
        self.assertFalse(owner.evidence()[-1]["cleanup_ok"])

    def test_nonempty_fixture_revokes_access_and_refuses_cleanup_acceptance(self):
        owner = self.create()
        paths = owner.paths
        owner.bind_outputs(self.output_manifest)
        owner.activate("test")
        self.assertEqual(self.actor(lambda: (paths["work"] / "leftover").touch()), 0)
        with self.assertRaisesRegex(ValueError, "cleanup-incomplete"):
            owner.deactivate()
        self.assertEqual(
            self.actor(lambda: (paths["work"] / "leftover").read_bytes()), errno.EACCES
        )
        self.assertFalse(owner.evidence()[-1]["cleanup_ok"])

    def test_busy_unmount_keeps_root_private_and_refuses_cleanup_acceptance(self):
        owner = self.create()
        paths = owner.paths
        descriptor = os.open(paths["runtime"] / "bin/tool", os.O_RDONLY)
        try:
            with self.assertRaisesRegex(ValueError, "cleanup-incomplete"):
                owner.close()
            self.assertEqual(
                self.actor(lambda: (paths["runtime"] / "alias").read_bytes()),
                errno.EACCES,
            )
            self.assertFalse(owner.evidence()[-1]["cleanup_ok"])
        finally:
            os.close(descriptor)
            target = paths["runtime"]
            if target.stat().st_dev != target.parent.stat().st_dev:
                views._command((owner._system.unmount, target))
