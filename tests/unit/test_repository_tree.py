"""Complete raw-tree hashes, portable paths and nonmutating prospective deltas."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import PurePosixPath

from literate_ai.contracts.repository_tree import (
    RepositoryTreeCapturePolicy,
    RepositoryTreeChange,
    RepositoryTreeEntry,
    RepositoryTreeSnapshot,
    repository_tree_changes,
)


def oid(kind, content, width=40):
    return hashlib.new(
        "sha1" if width == 40 else "sha256",
        kind.encode() + b" " + str(len(content)).encode() + b"\0" + content,
    ).hexdigest()


def tree(files, *, width=40, message=b"fixture"):
    entries = {}
    for path, (mode, content) in files.items():
        entries[path] = RepositoryTreeEntry(
            path,
            mode,
            "1" * width if mode == "160000" else oid("blob", content, width),
            None if mode == "160000" else content,
        )
    directories = {
        parent.as_posix() for path in files for parent in PurePosixPath(path).parents
    }
    directories.add(".")
    for directory in sorted(
        directories, key=lambda path: (-len(PurePosixPath(path).parts), path)
    ):
        children = [
            entry
            for entry in entries.values()
            if PurePosixPath(entry.path).parent.as_posix() == directory
        ]
        children.sort(
            key=lambda entry: (
                PurePosixPath(entry.path).name + ("/" if entry.mode == "040000" else "")
            ).encode()
        )
        raw = b"".join(
            entry.mode.lstrip("0").encode()
            + b" "
            + PurePosixPath(entry.path).name.encode()
            + b"\0"
            + bytes.fromhex(entry.object_id)
            for entry in children
        )
        identity = oid("tree", raw, width)
        if directory != ".":
            entries[directory] = RepositoryTreeEntry(directory, "040000", identity)
    commit = b"tree " + identity.encode() + b"\n\n" + message
    return RepositoryTreeSnapshot(
        oid("commit", commit, width),
        commit,
        tuple(sorted(entries.values(), key=lambda entry: entry.path)),
    )


class RepositoryTreeTests(unittest.TestCase):
    def test_empty_and_nested_trees_are_immutable_and_support_both_object_formats(self):
        for width in (40, 64):
            empty = tree({}, width=width)
            self.assertEqual(empty.entries, ())
            value = tree(
                {"dir/file": ("100644", b"a\0b"), "dir.c": ("100755", b"run")},
                width=width,
            )
            self.assertEqual(len(value.commit), width)
            self.assertEqual(value.identity, replace(value).identity)
            with self.assertRaises(FrozenInstanceError):
                value.commit = "1" * width

    def test_changed_commit_bytes_are_rejected(self):
        value = tree({})
        with self.assertRaises(ValueError):
            replace(value, commit_content=value.commit_content + b"tampered")

    def test_blob_content_must_match_its_exact_object_identity(self):
        value = tree({"file": ("100644", b"original")})
        with self.assertRaises(ValueError):
            replace(value.entries[0], content=b"different")

    def test_omitted_extra_or_mode_changed_entries_cannot_match_a_complete_tree(self):
        value = tree({"dir/file": ("100644", b"original"), "other": ("100644", b"b")})
        variants = (
            value.entries[:-1],
            value.entries[1:],
            (
                *value.entries,
                RepositoryTreeEntry("z", "100644", oid("blob", b"z"), b"z"),
            ),
            tuple(
                replace(entry, mode="100755") if entry.path == "other" else entry
                for entry in value.entries
            ),
        )
        for entries in variants:
            with self.assertRaises(ValueError):
                replace(value, entries=entries)

    def test_portable_aliases_and_unsafe_paths_are_rejected(self):
        for path in (
            "../escape",
            "/absolute",
            ".git/config",
            ".Git/config",
            "NUL.txt",
            "a\\b",
            "C:relative",
            "a ",
        ):
            with self.assertRaises((ValueError, TypeError)):
                tree({path: ("100644", b"x")})
        with self.assertRaises(ValueError):
            tree({"A": ("100644", b"a"), "a": ("100644", b"b")})

    def test_raw_symlink_and_gitlink_records_are_not_materialization_authority(self):
        value = tree({"link": ("120000", b"../../outside"), "child": ("160000", None)})
        self.assertEqual(value.entries[0].mode, "160000")
        self.assertIsNone(value.entries[0].content)
        self.assertEqual(value.entries[1].content, b"../../outside")

    def test_delta_records_changes_without_replacing_existing_directories(
        self,
    ):
        before = tree(
            {
                "dir/file": ("100644", b"old"),
                "remove": ("100644", b"old"),
                "run": ("100644", b"same"),
            }
        )
        after = tree(
            {
                "dir/file": ("100644", b"new"),
                "new": ("120000", b"dir/file"),
                "run": ("100755", b"same"),
            }
        )
        changes = repository_tree_changes(before, after)
        self.assertEqual(
            tuple(change.path for change in changes),
            ("dir/file", "new", "remove", "run"),
        )
        self.assertIsNone(changes[1].previous)
        self.assertIsNone(changes[2].prospective)
        self.assertEqual(changes[3].previous.content, changes[3].prospective.content)

    def test_same_tree_different_commit_is_a_noop_source_delta(self):
        before = tree({"file": ("100644", b"same")}, message=b"one")
        after = tree({"file": ("100644", b"same")}, message=b"two")
        self.assertNotEqual(before.identity, after.identity)
        self.assertEqual(repository_tree_changes(before, after), ())
        with self.assertRaises(ValueError):
            repository_tree_changes(before, tree({}, width=64))

    def test_capture_policy_rejects_boolean_zero_and_unbounded_limits(self):
        for value in (True, 0, -1, 8193, 1.5):
            with self.assertRaises(ValueError):
                RepositoryTreeCapturePolicy(maximum_entries=value)
        with self.assertRaises(ValueError):
            RepositoryTreeCapturePolicy(maximum_blob_bytes=16 * 1024 * 1024 + 1)

    def test_snapshots_require_typed_canonical_immutable_entries(self):
        value = tree({"a": ("100644", b"a"), "b": ("100644", b"b")})
        for entries in (list(value.entries), tuple(reversed(value.entries)), (None,)):
            with self.assertRaises((ValueError, TypeError)):
                replace(value, entries=entries)

    def test_change_records_reject_absent_equal_or_wrong_path_members(self):
        entry = tree({"file": ("100644", b"x")}).entries[0]
        for previous, prospective in ((None, None), (entry, entry), ("invalid", None)):
            with self.assertRaises(ValueError):
                RepositoryTreeChange("file", previous, prospective)
        with self.assertRaises(ValueError):
            RepositoryTreeChange("other", None, entry)
