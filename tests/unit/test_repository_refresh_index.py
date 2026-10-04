"""Native Git validates prospective index bytes without changing live indexes."""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_index import (
    refresh_root_index_bytes,
    tree_index_bytes,
)
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support.fixtures_test_repository_orchestration import git, repository, snapshot
from tests.support.fixtures_test_repository_tree import tree


class RefreshIndexTests(unittest.TestCase):
    def test_native_git_reads_tree_indexes_in_both_object_formats(self):
        for width in (40, 64):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                root = base / "repo"
                repository(root, sha256=width == 64)
                value = tree(
                    {
                        "binary": ("100644", b"a\0b"),
                        "dir/run": ("100755", b"run"),
                        "link": ("120000", b"../inert"),
                        "nested": ("160000", b""),
                    },
                    width=width,
                )
                stage = base / "index"
                stage.write_bytes(tree_index_bytes(value))
                before = snapshot(root)
                with patch.dict(
                    os.environ,
                    {"GIT_INDEX_FILE": str(stage), "GIT_OPTIONAL_LOCKS": "0"},
                ):
                    result = git(root, "ls-files", "--stage", "-z")
                expected = b"".join(
                    f"{entry.mode} {entry.object_id} 0\t{entry.path}".encode() + b"\0"
                    for entry in value.entries
                    if entry.mode != "040000"
                )
                self.assertEqual(result, expected)
                self.assertEqual(snapshot(root), before)

    def test_root_patching_versions_formats_and_unrelated_flags(self):
        for width in (40, 64):
            for version in (2, 3, 4):
                with (
                    self.subTest(width=width, version=version),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    base = Path(temporary).resolve()
                    root = base / "repo"
                    repository(root, sha256=width == 64)
                    for path in ("app", "application"):
                        git(
                            root,
                            "update-index",
                            "--add",
                            "--cacheinfo",
                            f"160000,{'1' * width},{path}",
                        )
                    if version == 3:
                        git(root, "update-index", "--skip-worktree", "source.txt")
                    git(root, "update-index", f"--index-version={version}")
                    original = (root / ".git/index").read_bytes()
                    self.assertEqual(int.from_bytes(original[4:8]), version)
                    request = RepositoryRefreshRequest(
                        (RepositoryRefreshTarget("app", "2" * width),)
                    )
                    staged = refresh_root_index_bytes(
                        original, request, object_width=width
                    )
                    path = base / "index"
                    path.write_bytes(staged)
                    before = snapshot(root)
                    with patch.dict(
                        os.environ,
                        {"GIT_INDEX_FILE": str(path), "GIT_OPTIONAL_LOCKS": "0"},
                    ):
                        result = git(root, "ls-files", "--stage").decode()
                        flags = git(root, "ls-files", "-v").decode()
                    self.assertIn(f"160000 {'2' * width} 0\tapp\n", result)
                    self.assertIn(f"160000 {'1' * width} 0\tapplication\n", result)
                    if version == 3:
                        self.assertIn("S source.txt", flags)
                    self.assertEqual(snapshot(root), before)

    def test_corruption_missing_or_non_gitlink_targets_and_extensions_refuse(self):
        raw = tree_index_bytes(
            tree({"app": ("160000", b""), "source": ("100644", b"x")})
        )
        request = RepositoryRefreshRequest((RepositoryRefreshTarget("app", "2" * 40),))
        for candidate in (raw[:-1], raw[:20] + b"x" + raw[21:]):
            with self.assertRaises(OrchestrationInventoryError):
                refresh_root_index_bytes(candidate, request, object_width=40)
        for path in ("missing", "source"):
            other = RepositoryRefreshRequest((RepositoryRefreshTarget(path, "2" * 40),))
            with self.assertRaises(OrchestrationInventoryError):
                refresh_root_index_bytes(raw, other, object_width=40)
        for signature in (b"link", b"sdir", b"ZZZZ"):
            content = raw[:-20] + signature + struct.pack("!I", 0)
            candidate = content + hashlib.sha1(content).digest()
            with self.assertRaises(OrchestrationInventoryError):
                refresh_root_index_bytes(candidate, request, object_width=40)

    def test_recovery_extension_is_retained_and_derived_cache_is_removed(self):
        raw = tree_index_bytes(tree({"app": ("160000", b"")}))[:-20]
        recovery = b"REUC" + struct.pack("!I", 0)
        content = raw + recovery + b"TREE" + struct.pack("!I", 0)
        request = RepositoryRefreshRequest((RepositoryRefreshTarget("app", "2" * 40),))
        result = refresh_root_index_bytes(
            content + hashlib.sha1(content).digest(), request, object_width=40
        )
        self.assertEqual(result[-28:-20], recovery)
        self.assertEqual(len(result), len(raw) + len(recovery) + 20)

    def test_noop_retains_exact_index_and_target_hidden_flags_refuse(self):
        original = tree_index_bytes(tree({"app": ("160000", b"")}))
        request = RepositoryRefreshRequest((RepositoryRefreshTarget("app", "1" * 40),))
        self.assertEqual(
            refresh_root_index_bytes(original, request, object_width=40), original
        )
        raw = bytearray(original[:-20])
        # The first entry's assume-valid bit must not hide a selected Gitlink.
        raw[12 + 60] |= 0x80
        with self.assertRaises(OrchestrationInventoryError):
            refresh_root_index_bytes(
                bytes(raw) + hashlib.sha1(raw).digest(), request, object_width=40
            )
