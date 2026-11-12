"""Explicit native budgets keep streaming byte, membership and alias custody."""

import hashlib
import os
import tempfile
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.retained_cargo_build_outputs import (
    capture_retained_cargo_build_outputs,
)
from literate_ai.adapters.retained_cargo_runtime_files import (
    capture_retained_cargo_runtime_files,
)
from literate_ai.adapters.streamed_file_custody import StreamedFileCustody
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo_custody import (
    RetainedCargoCustodyPolicy,
    RetainedCargoInputBudget,
    RetainedCargoOwnerBudget,
)
from literate_ai.contracts.retained_cargo_native_custody import (
    RetainedCargoNativeCustodyPolicy,
    RetainedCargoOutputBudget,
)
from literate_ai.contracts.retained_cargo_policy import RetainedLibraryGatePolicyV2
from literate_ai.contracts.retained_libraries import RetainedLibraryGatePolicy
from literate_ai.projects import PinnedInputClosure, PinnedInputClosureError


class RetainedCargoNativePolicyTests(unittest.TestCase):
    def policy(self):
        budget = RetainedCargoInputBudget(10, 20, 1024, 4096)
        return RetainedLibraryGatePolicyV2(
            RetainedLibraryGatePolicy(
                "fixture",
                (
                    RepositoryBuildCommand(
                        "check", ("make", "check"), ".", (), network=False
                    ),
                ),
                (canonical_identity({"tool": 1}),),
            ),
            RetainedCargoCustodyPolicy(
                tuple(
                    RetainedCargoOwnerBudget(name, budget)
                    for name in ("cargo-home", "workspace")
                ),
                budget,
            ),
        )

    def test_legacy_policy_wire_and_identity_are_unchanged(self):
        policy = self.policy()
        expected = policy.common.to_dict()
        expected.update(
            schema=policy.SCHEMA,
            external_input_variables=[],
            input_custody=policy.input_custody.to_dict(),
        )
        self.assertEqual(policy.to_dict(), expected)
        self.assertEqual(policy.identity, canonical_identity(expected))
        self.assertEqual(type(policy).from_dict(expected), policy)

    def test_reviewed_budgets_roundtrip_and_change_policy_identity(self):
        old = self.policy()
        budget = RetainedCargoOutputBudget(1024, 4096)
        native = RetainedCargoNativeCustodyPolicy(budget, budget, budget)
        policy = replace(old, native_custody=native)
        self.assertEqual(type(policy).from_dict(policy.to_dict()), policy)
        self.assertNotEqual(policy.identity, old.identity)
        changed = replace(native, executables=RetainedCargoOutputBudget(1025, 4096))
        self.assertNotEqual(
            replace(policy, native_custody=changed).identity, policy.identity
        )
        for invalid in (None, {}, {**native.to_dict(), "unreviewed": 1}):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                type(policy).from_dict({**old.to_dict(), "native_custody": invalid})
        with self.assertRaises(ValueError):
            RetainedLibraryGatePolicy.from_dict(policy.to_dict())

    def test_budget_hard_bounds_and_unknown_fields(self):
        for member, total in (
            (True, 4),
            (0, 4),
            (5, 4),
            (1024**3 + 1, 2 * 1024**3),
            (1, 64 * 1024**3 + 1),
        ):
            with (
                self.subTest(member=member, total=total),
                self.assertRaises(ValueError),
            ):
                RetainedCargoOutputBudget(member, total)
        with self.assertRaises(ValueError):
            RetainedCargoOutputBudget.from_dict(
                {"maximum_file_bytes": 1, "maximum_total_bytes": 2, "omit": []}
            )
        with self.assertRaises(ValueError):
            budget = RetainedCargoOutputBudget(1024**3, 32 * 1024**3)
            RetainedCargoNativeCustodyPolicy(budget, budget, budget)


class StreamedNativeCustodyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="nc-"))
        ).resolve()

    def closure(self, member=1024, total=4096, files=10):
        return StreamedFileCustody(
            maximum_files=files, budget=RetainedCargoOutputBudget(member, total)
        )

    def test_legacy_identity_parity_and_same_file_labels(self):
        file = self.root / "binary"
        file.write_bytes(b"complete bytes")
        old, new = PinnedInputClosure(), self.closure()
        for label in ("a", "b"):
            old.pin(file, boundary=self.root, label=label)
            ref = new.pin(file, boundary=self.root, label=label)
        self.assertEqual(ref.digest, hashlib.sha256(file.read_bytes()).hexdigest())
        self.assertEqual(old.identity, new.identity)
        self.assertEqual((new.file_count, new.total_bytes), (1, file.stat().st_size))
        new.require_unchanged()

    def test_large_member_uses_bounded_memory_and_requires_explicit_allowance(self):
        file = self.root / "large"
        size = 65 * 1024**2
        with file.open("wb") as stream:
            stream.truncate(size)
        with self.assertRaises(PinnedInputClosureError):
            PinnedInputClosure(maximum_file_bytes=64 * 1024**2).pin(
                file, boundary=self.root, label="large"
            )
        captured = self.closure(member=size, total=size)
        tracemalloc.start()
        try:
            captured.pin(file, boundary=self.root, label="large")
            captured.require_unchanged()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(captured.total_bytes, size)
        self.assertLess(peak, 8 * 1024**2)

    def test_member_aggregate_file_and_label_limits_refuse(self):
        first, second = self.root / "a", self.root / "b"
        first.write_bytes(b"1234")
        second.write_bytes(b"5678")
        with self.assertRaisesRegex(ValueError, "byte-limit"):
            self.closure(3, 8).pin(first, boundary=self.root, label="a")
        total = self.closure(4, 7)
        total.pin(first, boundary=self.root, label="a")
        with self.assertRaisesRegex(ValueError, "byte-limit"):
            total.pin(second, boundary=self.root, label="b")
        count = self.closure(files=1)
        count.pin(first, boundary=self.root, label="a")
        with self.assertRaisesRegex(ValueError, "file-limit"):
            count.pin(second, boundary=self.root, label="b")
        with self.assertRaisesRegex(ValueError, "label-limit"):
            count.pin(first, boundary=self.root, label="b")
        labels = self.closure()
        labels.pin(first, boundary=self.root, label="a")
        with self.assertRaisesRegex(ValueError, "label-conflict"):
            labels.pin(second, boundary=self.root, label="a")

    def test_fresh_byte_replacement_and_write_restore_refuse(self):
        for change in ("bytes", "replacement", "restore"):
            with self.subTest(change=change):
                file = self.root / change
                file.write_bytes(b"original")
                closure = self.closure()
                closure.pin(file, boundary=self.root, label="file")
                if change == "replacement":
                    file.rename(file.with_suffix(".old"))
                    file.write_bytes(b"original")
                else:
                    file.write_bytes(b"modified")
                    if change == "restore":
                        file.write_bytes(b"original")
                with self.assertRaises(ValueError):
                    closure.require_unchanged()

    @unittest.skipIf(os.name == "nt", "POSIX modes and symlinks")
    def test_permissions_ancestors_and_links_refuse(self):
        parent = self.root / "holder/parent"
        parent.mkdir(parents=True)
        file = parent / "file"
        file.write_bytes(b"original")
        captured = self.closure()
        captured.pin(file, boundary=self.root, label="file")
        file.chmod(0o600 if file.stat().st_mode & 0o777 != 0o600 else 0o644)
        with self.assertRaises(ValueError):
            captured.require_unchanged()
        captured = self.closure()
        captured.pin(file, boundary=self.root, label="file")
        file_before = file.stat()
        parent.parent.rename(self.root / "old")
        parent.parent.mkdir()
        # Move the containing directory, preserving every file stat field.
        # Only the grandparent inode changes in the captured lineage.
        (self.root / "old/parent").rename(parent)
        file_after = file.stat()
        self.assertEqual(file_before, file_after)
        self.assertEqual(file_before.st_ctime_ns, file_after.st_ctime_ns)
        with self.assertRaises(ValueError):
            captured.require_unchanged()
        target = self.root / "target"
        target.write_bytes(b"original")
        file.unlink()
        file.symlink_to(target)
        with self.assertRaises(ValueError):
            self.closure().pin(file, boundary=self.root, label="file")

    def test_midstream_same_size_rewrite_uses_existing_native_recheck(self):
        file = self.root / "file"
        file.write_bytes(b"original")
        read = os.read
        changed = False

        def mutate(fd, count):
            nonlocal changed
            data = read(fd, count)
            if data and not changed:
                changed = True
                file.write_bytes(b"modified")
            return data

        with patch("os.read", side_effect=mutate), self.assertRaises(ValueError):
            self.closure().pin(file, boundary=self.root, label="file")

    def test_runtime_complete_membership_and_default_identity(self):
        file = self.root / "library"
        file.write_bytes(b"exact")
        old = capture_retained_cargo_runtime_files((self.root,))
        captured = capture_retained_cargo_runtime_files(
            (self.root,), custody=RetainedCargoOutputBudget(1024, 4096)
        )
        self.assertEqual(captured.identity, old.identity)
        (self.root / "extra").write_bytes(b"unselected by no filename filter")
        with self.assertRaisesRegex(ValueError, "files-changed"):
            captured.require_unchanged()

    @unittest.skipIf(os.name == "nt", "POSIX file aliases")
    def test_runtime_external_alias_target_is_freshly_guarded(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        target = self.root / "actual"
        target.write_bytes(b"original")
        (runtime / "alias").symlink_to(target)
        captured = capture_retained_cargo_runtime_files(
            (runtime,),
            file_alias_roots=(runtime,),
            custody=RetainedCargoOutputBudget(1024, 4096),
        )
        captured.require_unchanged()
        target.write_bytes(b"modified")
        with self.assertRaises(ValueError):
            captured.require_unchanged()

    def test_recursive_out_dir_and_new_nested_member_are_guarded(self):
        output = self.root / "out"
        output.mkdir()
        nested = output / "nested"
        nested.mkdir()
        (nested / "generated").write_bytes(b"exact")
        args = (
            [
                {
                    "reason": "build-script-executed",
                    "package_id": "p",
                    "out_dir": str(output),
                }
            ],
            {"packages": [{"id": "p"}]},
        )
        old = capture_retained_cargo_build_outputs(*args, output_root=self.root)
        captured = capture_retained_cargo_build_outputs(
            *args, output_root=self.root, custody=RetainedCargoOutputBudget(1024, 4096)
        )
        self.assertEqual(captured.identity, old.identity)
        (nested / "extra").write_bytes(b"new")
        with self.assertRaisesRegex(ValueError, "changed"):
            captured.require_unchanged()
