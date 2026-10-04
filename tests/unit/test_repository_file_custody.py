"""Physical rollback inputs and foreign-entry refusal without worktree writes."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import repository_file_custody as custody
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy
from tests.support.fixtures_test_repository_tree import tree


class RepositoryFileCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        node = self.root.stat()
        self.root_node = (node.st_dev, node.st_ino, node.st_mode)

    def write(self, path, content=b"old\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def prepare(self, before, after, **options):
        return custody.prepare_worktree_changes(
            self.root, self.root_node, tree(before), tree(after), **options
        )

    def refuses(self, suffix, action):
        with self.assertRaises(OrchestrationInventoryError) as caught:
            action()
        self.assertEqual(caught.exception.code, "orchestration.refresh_files_" + suffix)

    def test_actual_crlf_bytes_and_foreign_siblings_are_preserved(self):
        source = self.write("source", b"old\r\n")
        foreign = self.write("ignored", b"private")
        initial = (source.stat(), foreign.stat())
        prepared = self.prepare(
            {"source": ("100644", b"old\n")},
            {"source": ("100755", b"new\n")},
        )
        self.assertEqual(prepared.nodes[0].content, b"old\r\n")
        self.assertEqual(prepared.physical_bytes, 5)
        self.assertEqual(len(prepared.directories[0].members), 2)
        self.assertEqual(prepared.identity, prepared.identity)
        custody.require_worktree_changes_unchanged(prepared)
        self.assertEqual(source.read_bytes(), b"old\r\n")
        self.assertEqual(foreign.read_bytes(), b"private")
        for before, after in zip(initial, (source.stat(), foreign.stat()), strict=True):
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(before.st_ino, after.st_ino)

    def test_identity_preserves_oversized_os_node_identifiers(self):
        self.write("source")
        prepared = self.prepare({"source": ("100644", b"old\n")}, {})
        oversized = replace(
            prepared,
            root_node=(2**63, *prepared.root_node[1:]),
        )
        changed = replace(
            prepared,
            root_node=(2**63 + 1, *prepared.root_node[1:]),
        )

        self.assertTrue(oversized.identity.startswith("sha256:"))
        self.assertNotEqual(oversized.identity, changed.identity)

    def test_addition_cannot_clobber_foreign_file_or_empty_directory(self):
        for kind in ("file", "directory"):
            with self.subTest(kind=kind):
                path = self.root / kind
                path.write_bytes(b"foreign") if kind == "file" else path.mkdir()
                self.refuses(
                    "collision",
                    lambda kind=kind: self.prepare({}, {kind: ("100644", b"new")}),
                )
                self.assertTrue(path.exists())

    def test_retiring_directory_refuses_nested_ignored_entries(self):
        self.write("dir/nested/old")
        foreign = self.write("dir/nested/ignored", b"foreign")
        before = {"dir/nested/old": ("100644", b"old\n")}
        for after in ({}, {"dir": ("100644", b"replacement")}):
            self.refuses("collision", lambda after=after: self.prepare(before, after))
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_owned_directory_can_be_replaced_without_following_descendants(self):
        self.write("dir/old")
        prepared = self.prepare(
            {"dir/old": ("100644", b"old\n")},
            {"dir": ("100644", b"new")},
        )
        self.assertEqual([node.kind for node in prepared.nodes], ["directory", "file"])
        custody.require_worktree_changes_unchanged(prepared)

    def test_directory_inventory_uses_fresh_no_follow_member_metadata(self):
        source = self.write("dir/old")
        scandir = os.scandir

        @contextmanager
        def cached_entries(path):
            with scandir(path) as entries:
                yield iter(
                    SimpleNamespace(name=entry.name, path=entry.path)
                    for entry in entries
                )

        # Windows enumeration metadata can be stale and lacks file identities.
        # The entry supplies only its name/path, never an authoritative stat.
        with patch.object(custody.os, "scandir", cached_entries):
            prepared = self.prepare(
                {"dir/old": ("100644", b"old\n")},
                {"dir": ("100644", b"new")},
            )
            custody.require_worktree_changes_unchanged(prepared)
        directory = next(item for item in prepared.directories if item.path == "dir")
        self.assertEqual(
            directory.members, ((b"old", custody._signature(source.lstat())),)
        )

    def test_foreign_member_same_bytes_and_timestamp_replacement_refuses(self):
        self.write("source")
        foreign = self.write("ignored", b"private")
        replacement = self.write("replacement", b"private")
        before = foreign.stat()
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        prepared = self.prepare({"source": ("100644", b"old\n")}, {})
        # Keep the replaced inode alive and directory membership unchanged.
        parked = self.root / "parked"
        foreign.rename(parked)
        replacement.rename(foreign)
        parked.rename(replacement)
        self.refuses(
            "changed", lambda: custody.require_worktree_changes_unchanged(prepared)
        )
        self.assertEqual(foreign.read_bytes(), b"private")

    def test_directory_inventory_does_not_follow_foreign_member_links(self):
        source = self.write("source")
        link = self.root / "foreign-link"
        try:
            link.symlink_to(source.name)
        except OSError:
            self.skipTest("host does not permit symlink fixtures")
        prepared = self.prepare({"source": ("100644", b"old\n")}, {})
        inventory = dict(prepared.directories[0].members)
        self.assertEqual(inventory[b"foreign-link"], custody._signature(link.lstat()))
        self.assertNotEqual(
            inventory[b"foreign-link"], custody._signature(source.stat())
        )

    def test_file_to_directory_descendants_are_logically_absent(self):
        self.write("dir")
        prepared = self.prepare(
            {"dir": ("100644", b"old\n")},
            {"dir/new": ("100644", b"new")},
        )
        self.assertEqual([node.kind for node in prepared.nodes], ["file", "absent"])
        self.assertEqual((self.root / "dir").read_bytes(), b"old\n")

    def test_link_to_directory_retains_raw_target_and_never_reads_target(self):
        target = self.write("outside/new", b"foreign")
        link = self.root / "link"
        try:
            link.symlink_to("outside", target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlink fixtures")
        prepared = self.prepare(
            {"link": ("120000", b"outside")},
            {"link/new": ("100644", b"new")},
        )
        self.assertEqual(prepared.nodes[0].content, b"outside")
        self.assertEqual(prepared.nodes[1].kind, "absent")
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_unowned_link_ancestor_refuses(self):
        self.write("outside/old")
        try:
            (self.root / "dir").symlink_to("outside", target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlink fixtures")
        self.refuses(
            "unsafe",
            lambda: self.prepare(
                {"dir/old": ("100644", b"old\n")},
                {"dir/old": ("100644", b"new")},
            ),
        )

    def test_hardlinked_and_special_nodes_refuse(self):
        source = self.write("source")
        try:
            os.link(source, self.root / "alias")
        except OSError:
            self.skipTest("host does not permit hardlink fixtures")
        self.refuses(
            "unsafe", lambda: self.prepare({"source": ("100644", b"old\n")}, {})
        )
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")
            self.refuses("unsafe", lambda: self.prepare({"pipe": ("100644", b"")}, {}))

    def test_case_alias_refuses_even_for_case_only_rename(self):
        self.write("Source")
        with self.assertRaises(OrchestrationInventoryError):
            self.prepare(
                {"Source": ("100644", b"old\n")},
                {"source": ("100644", b"new")},
            )
        self.assertEqual((self.root / "Source").read_bytes(), b"old\n")

    def test_node_replacement_and_clock_drift_invalidate_saved_custody(self):
        source = self.write("source")
        before = {"source": ("100644", b"old\n")}
        after = {"source": ("100644", b"new")}
        prepared = self.prepare(before, after)
        replacement = self.write("replacement")
        os.replace(replacement, source)
        self.refuses(
            "changed", lambda: custody.require_worktree_changes_unchanged(prepared)
        )
        prepared = self.prepare(before, after)
        node = source.stat()
        os.utime(source, ns=(node.st_atime_ns, node.st_mtime_ns + 1_000_000_000))
        self.refuses(
            "changed", lambda: custody.require_worktree_changes_unchanged(prepared)
        )

    def test_foreign_sibling_drift_invalidates_plan_without_removal(self):
        self.write("source")
        prepared = self.prepare({"source": ("100644", b"old\n")}, {})
        foreign = self.write("ignored", b"foreign")
        self.refuses(
            "changed", lambda: custody.require_worktree_changes_unchanged(prepared)
        )
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_physical_per_file_aggregate_and_directory_bounds(self):
        self.write("a", b"1234")
        self.write("b", b"5678")
        before = {path: ("100644", b"x") for path in ("a", "b")}
        for policy in (
            RepositoryTreeCapturePolicy(maximum_blob_bytes=3),
            RepositoryTreeCapturePolicy(maximum_total_blob_bytes=7),
            RepositoryTreeCapturePolicy(maximum_entries=1),
        ):
            self.refuses(
                "limit", lambda policy=policy: self.prepare(before, {}, policy=policy)
            )
        self.write("foreign")
        self.refuses(
            "limit",
            lambda: self.prepare(
                before, {}, policy=RepositoryTreeCapturePolicy(maximum_entries=2)
            ),
        )

    def test_transaction_metadata_and_nested_gitlink_transitions_refuse(self):
        for path in (".litai-locks/x", ".literate/repository.lock.json", ".literate"):
            self.refuses(
                "reserved", lambda path=path: self.prepare({}, {path: ("100644", b"x")})
            )
        self.refuses(
            "nested_ownership", lambda: self.prepare({}, {"nested": ("160000", b"")})
        )

    def test_noop_leaves_independent_gitlinks_and_foreign_files_unobserved(self):
        before = {"nested": ("160000", b"")}
        self.write("foreign")
        with patch.object(
            custody, "_directory", side_effect=AssertionError("inventory")
        ):
            prepared = self.prepare(before, before)
        self.assertEqual(prepared.nodes, ())
        self.assertEqual(prepared.directories, ())

    def test_preparation_detects_drift_between_its_two_observations(self):
        source = self.write("source")
        observe = custody._observe_worktree_changes
        calls = []

        def drifting(*args, **options):
            result = observe(*args, **options)
            calls.append(result)
            if len(calls) == 1:
                source.write_bytes(b"foreign")
            return result

        with patch.object(custody, "_observe_worktree_changes", drifting):
            self.refuses(
                "changed", lambda: self.prepare({"source": ("100644", b"old\n")}, {})
            )
        self.assertEqual(source.read_bytes(), b"foreign")

    def test_missing_file_and_replaced_root_refuse(self):
        self.refuses("changed", lambda: self.prepare({"missing": ("100644", b"x")}, {}))
        self.root_node = (self.root_node[0], self.root_node[1] + 1, self.root_node[2])
        self.refuses("changed", lambda: self.prepare({}, {}))

    @unittest.skipIf(os.name == "nt", "POSIX executable mode semantics")
    def test_mode_drift_invalidates_custody(self):
        source = self.write("source")
        source.chmod(0o644)
        prepared = self.prepare({"source": ("100644", b"old\n")}, {})
        source.chmod(0o755)
        self.refuses(
            "changed", lambda: custody.require_worktree_changes_unchanged(prepared)
        )
