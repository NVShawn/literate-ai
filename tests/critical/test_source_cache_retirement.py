"""Deliberate retirement preserves history without accepting unexplained loss."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache import FileSystemSourceCache, SourceCacheError
from literate_ai.adapters.source_materialization import (
    capture_accepted_source_cache_archive,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.remote_source_guard import extract_accepted_source_cache_archive
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_source_cache import _accepted_entry, _cache_key


class SourceCacheRetirementTests(unittest.TestCase):
    def populate(self, root):
        cas = FileSystemCAS(root.parent / "caller-cas")
        key = _cache_key()
        first = _accepted_entry(cas, suffix="first", key=key)
        second = _accepted_entry(cas, suffix="second", key=key)
        cache = FileSystemSourceCache("current", root)
        for entry in (first, second):
            cache.publish(entry, caller_cas=cas)
        return cache, key, first, second

    def test_retirement_keeps_immutable_history_and_read_only_current_enumeration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cache, key, first, second = self.populate(root / "cache")
            original = {p.name: p.read_bytes() for p in cache.entries.glob("*.json")}
            cache.retire_other_memberships(key, keep=second.identity)
            self.assertEqual(cache.candidates(key), (second,))
            self.assertCountEqual(cache.published_entries(), (first, second))
            self.assertEqual(cache.verified_published_entries(), (second,))
            self.assertEqual(
                original, {p.name: p.read_bytes() for p in cache.entries.glob("*.json")}
            )
            before = {
                p.relative_to(cache.root): p.read_bytes()
                for p in cache.root.rglob("*")
                if p.is_file()
            }
            reader = FileSystemSourceCache("reader", cache.root, writable=False)
            self.assertEqual(reader.verified_published_entries(), (second,))
            self.assertEqual(
                before,
                {
                    p.relative_to(cache.root): p.read_bytes()
                    for p in cache.root.rglob("*")
                    if p.is_file()
                },
            )
            # The retired record cannot excuse deletion of the current pointer.
            (
                cache.keys
                / key.accepted_source_lookup_identity.digest
                / f"{second.identity.digest}.json"
            ).unlink()
            with self.assertRaises(SourceCacheError):
                reader.verified_published_entries()

    def test_missing_or_substituted_retirement_record_is_not_accepted(self):
        for mutation in ("missing", "wrong-key"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                cache, key, first, second = self.populate(
                    Path(directory).resolve() / "cache"
                )
                cache.retire_other_memberships(key, keep=second.identity)
                marker = (
                    cache.keys
                    / key.accepted_source_lookup_identity.digest
                    / ".retired"
                    / f"{first.identity.digest}.json"
                )
                if mutation == "missing":
                    marker.unlink()
                else:
                    value = json.loads(marker.read_bytes())
                    value["accepted_source_lookup_identity"] = "sha256:" + "0" * 64
                    marker.write_bytes(canonical_json_bytes(value))
                with self.assertRaises(SourceCacheError):
                    cache.verified_published_entries()

    def test_retired_history_transports_and_can_be_explicitly_reactivated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            build = project / "generated"
            cache, key, first, second = self.populate(build / "accepted-source-cache")
            cache.retire_other_memberships(key, keep=second.identity)
            staging = root / "staging"
            staging.mkdir()
            with patch.dict(os.environ, {"BUILD_DIR": str(build)}):
                self.assertIsNotNone(
                    capture_accepted_source_cache_archive(project, directory=staging)
                )
            destination = root / "restored"
            extract_accepted_source_cache_archive(
                staging / "accepted-source-cache.tar.gz", destination
            )
            restored = FileSystemSourceCache("restored", destination)
            self.assertEqual(restored.verified_published_entries(), (second,))
            restored.publish(first, caller_cas=cache.cas)
            self.assertCountEqual(restored.candidates(key), (first, second))
            restored.retire_other_memberships(key, keep=first.identity)
            self.assertEqual(restored.verified_published_entries(), (first,))


if __name__ == "__main__":
    unittest.main()
