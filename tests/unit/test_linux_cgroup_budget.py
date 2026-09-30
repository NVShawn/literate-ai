"""Cgroup inspection fixtures test parsing/custody, not kernel enforcement."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import linux_cgroup_budget as cgroup
from literate_ai.security.isolation.contracts import IsolationPolicyError


class CgroupBudgetContractTests(unittest.TestCase):
    def test_limits_reject_unbounded_and_noninteger_values(self):
        limits = cgroup.CgroupV2Limits(100000, 100000, 1048576, 0, 16)
        for name in ("cpu_quota_us", "cpu_period_us", "memory_bytes", "processes"):
            for value in (0, -1, True, 1.5, 2**63, "max"):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    replace(limits, **{name: value})
        for value in (-1, False, "max", 2**63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(limits, swap_bytes=value)

    def test_mount_is_bound_to_open_directory_descriptor(self):
        info = b"pos:\t0\nmnt_id:\t44\n"
        mount = b"44 1 0:29 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        self.assertEqual(cgroup._require_cgroup_mount(info, mount), 44)
        for other_info, other_mount in (
            (info, mount.replace(b"cgroup2", b"tmpfs")),
            (info, mount.replace(b"44 1", b"45 1")),
            (info, mount + mount),
            (info + b"mnt_id:\t44\n", mount),
            (b"mnt_id:\tbad\n", mount),
            (info, b"44 - cgroup2\n"),
            (b"x" * 4097, mount),
            (info, b"x" * (1024 * 1024 + 1)),
        ):
            with (
                self.subTest(info=other_info[:40]),
                self.assertRaises(IsolationPolicyError),
            ):
                cgroup._require_cgroup_mount(other_info, other_mount)

    def test_other_platforms_refuse_before_opening_files(self):
        with (
            patch.object(cgroup.sys, "platform", "win32"),
            patch.object(cgroup, "_open_directory") as opening,
        ):
            with self.assertRaises(IsolationPolicyError):
                cgroup.inspect_cgroup_v2_budget(
                    Path("/irrelevant"), cgroup.CgroupV2Limits(1, 1, 1, 0, 1)
                )
            opening.assert_not_called()


@unittest.skipUnless(os.name == "posix", "descriptor-relative fixture needs POSIX")
class CgroupBudgetInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "leaf"
        self.path.mkdir()
        self.limits = cgroup.CgroupV2Limits(100000, 100000, 1048576, 0, 16)
        values = {
            "cpu.max": "100000 100000\n",
            "cpu.max.burst": "0\n",
            "memory.max": "1048576\n",
            "memory.swap.max": "0\n",
            "memory.oom.group": "1\n",
            "pids.max": "16\n",
            "cgroup.type": "domain\n",
            "cgroup.subtree_control": "",
            "cgroup.procs": "",
            "cgroup.kill": "",
            "cgroup.controllers": "cpu memory pids\n",
            "cgroup.events": "populated 0\nfrozen 0\n",
        }
        for name, content in values.items():
            (self.path / name).write_text(content)

    def inspect(self, path=None):
        # Explicitly simulated filesystem identity; never an enforcement proof.
        with (
            patch.object(cgroup.sys, "platform", "linux"),
            patch.object(cgroup, "_mount_id", return_value=44),
        ):
            return cgroup.inspect_cgroup_v2_budget(path or self.path, self.limits)

    def test_exact_empty_leaf_passes_without_mutation(self):
        before = {path.name: path.read_bytes() for path in self.path.iterdir()}
        snapshot = self.inspect()
        self.assertEqual(snapshot.inode, self.path.stat().st_ino)
        self.assertEqual(snapshot.mount_id, 44)
        self.assertEqual(snapshot.identity, self.inspect().identity)
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.path.iterdir()}
        )

    def test_missing_unlimited_populated_or_threaded_state_refuses(self):
        for name, content in (
            ("cpu.max", "max 100000\n"),
            ("cpu.max.burst", "1000\n"),
            ("memory.max", "max\n"),
            ("memory.swap.max", "max\n"),
            ("pids.max", "max\n"),
            ("memory.oom.group", "0\n"),
            ("cgroup.type", "threaded\n"),
            ("cgroup.procs", "123\n"),
            ("cgroup.controllers", "cpu pids\n"),
            ("cgroup.events", "populated 1\nfrozen 0\n"),
            ("cgroup.events", "populated 0\nfrozen 1\n"),
            ("cgroup.events", "populated 0\npopulated 0\nfrozen 0\n"),
            ("cgroup.procs", "x" * 4097),
        ):
            target = self.path / name
            original = target.read_bytes()
            try:
                target.write_text(content)
                with self.subTest(name=name), self.assertRaises(IsolationPolicyError):
                    self.inspect()
            finally:
                target.write_bytes(original)
        for target in tuple(self.path.iterdir()):
            original = target.read_bytes()
            target.unlink()
            try:
                with (
                    self.subTest(missing=target.name),
                    self.assertRaises(IsolationPolicyError),
                ):
                    self.inspect()
            finally:
                target.write_bytes(original)

        (self.path / "child").mkdir()
        with self.assertRaises(IsolationPolicyError):
            self.inspect()
        (self.path / "child").rmdir()

    def test_links_and_regular_files_cannot_impersonate_a_cgroup(self):
        alias = self.path.parent / "alias"
        alias.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(IsolationPolicyError):
            self.inspect(alias)
        target = self.path / "memory.max"
        external = self.path.parent / "external"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
        with self.assertRaises(IsolationPolicyError):
            self.inspect()
        with patch.object(cgroup.sys, "platform", "linux"):
            with self.assertRaises(IsolationPolicyError):
                cgroup.inspect_cgroup_v2_budget(self.path, self.limits)

    def test_changed_settings_between_passes_refuse(self):
        read = cgroup._read_setting
        count = 0

        def changing(fd, name):
            nonlocal count
            if name == "cpu.max":
                count += 1
                if count == 2:
                    return "max 100000\n"
            return read(fd, name)

        with patch.object(cgroup, "_read_setting", side_effect=changing):
            with self.assertRaises(IsolationPolicyError):
                self.inspect()
