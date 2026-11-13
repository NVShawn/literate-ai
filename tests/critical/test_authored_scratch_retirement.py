"""Original-owner retirement controls; Python filesystem code only.

Namespace custody is cooperative. These tests do not claim hostile same-UID
containment or native Windows validation from a POSIX run.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import authored_qualification_publication as publication
from literate_ai.adapters import qualification_workspace_namespace as namespace
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes


class AuthoredScratchRetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.scratch = publication._FreshScratch(self.parent)

    def test_normal_retirement_removes_original_run_and_retains_empty_container(self):
        child = self.scratch.path / "nested"
        child.mkdir()
        (child / "output").write_text("owned")
        identity = self.scratch.retire()
        self.assertEqual(identity, list(self.scratch.identity))
        self.assertFalse(self.scratch.active)
        self.assertFalse(self.scratch.path.exists())
        self.assertTrue(self.scratch.container.is_dir())
        self.assertEqual(list(self.scratch.container.iterdir()), [])
        with self.assertRaisesRegex(ValueError, "owner_expired"):
            self.scratch.retire()

    def test_replacement_before_retirement_preserves_both_trees(self):
        original = self.scratch.container / "original"
        (self.scratch.path / "owned").write_text("original")
        self.scratch.path.rename(original)
        self.scratch.path.mkdir()
        marker = self.scratch.path / "foreign"
        marker.write_text("foreign")
        with self.assertRaises(ValueError):
            self.scratch.retire()
        self.assertTrue(self.scratch.active)
        self.assertEqual(marker.read_text(), "foreign")
        self.assertEqual((original / "owned").read_text(), "original")

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor retirement")
    def test_public_parent_replacement_after_guard_cannot_delete_foreign_tree(self):
        original = self.parent / "moved-container"
        (self.scratch.path / "owned").write_text("original")
        real = namespace._retire_posix_run

        def replace_then_retire(path, identity, container, check):
            container.rename(original)
            container.mkdir(mode=0o700)
            path.mkdir()
            (path / "foreign").write_text("foreign")
            return real(path, identity, container, check)

        with patch.object(namespace, "_retire_posix_run", replace_then_retire):
            with self.assertRaises(ValueError):
                self.scratch.retire()
        self.assertTrue(self.scratch.active)
        self.assertEqual((self.scratch.path / "foreign").read_text(), "foreign")
        self.assertEqual((original / "run/owned").read_text(), "original")

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor retirement")
    def test_run_replacement_after_open_is_preserved_and_never_claimed_retired(self):
        original = self.scratch.container / "moved-run"
        (self.scratch.path / "owned").write_text("owned")
        real = os.scandir
        replaced = False

        def replace_after_open(directory):
            nonlocal replaced
            if isinstance(directory, int) and not replaced:
                node = os.fstat(directory)
                if (node.st_dev, node.st_ino) == self.scratch.identity:
                    replaced = True
                    self.scratch.path.rename(original)
                    self.scratch.path.mkdir()
                    (self.scratch.path / "foreign").write_text("foreign")
            return real(directory)

        with patch.object(namespace.os, "scandir", replace_after_open):
            with self.assertRaisesRegex(ValueError, "scratch_owner_changed"):
                self.scratch.retire()
        self.assertTrue(replaced)
        self.assertTrue(self.scratch.active)
        self.assertEqual((self.scratch.path / "foreign").read_text(), "foreign")
        self.assertTrue(original.is_dir())

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor retirement")
    def test_interrupted_retirement_preserves_live_owner_and_diagnostics(self):
        marker = self.scratch.path / "owned"
        marker.write_text("owned")
        with patch.object(namespace.os, "unlink", side_effect=InterruptedError("stop")):
            with self.assertRaises(InterruptedError):
                self.scratch.retire()
        self.assertTrue(self.scratch.active)
        self.assertEqual(marker.read_text(), "owned")
        self.scratch.retire()
        self.assertFalse(self.scratch.path.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX symlink and permission behavior")
    def test_readonly_directories_and_links_do_not_modify_external_objects(self):
        external = self.parent / "external"
        external.write_text("untouched")
        nested = self.scratch.path / "readonly"
        nested.mkdir()
        (nested / "owned").write_text("owned")
        (self.scratch.path / "link").symlink_to(external)
        nested.chmod(0o500)
        self.scratch.retire()
        self.assertEqual(external.read_text(), "untouched")

    def records(self):
        plan_id, origin_id = canonical_identity("plan"), canonical_identity("origin")
        current = SimpleNamespace(
            plan=SimpleNamespace(
                output_root="q",
                identity=plan_id,
                origin=SimpleNamespace(identity=origin_id),
            ),
            oracles=object(),
        )
        attempt = "a" * 32
        intent = {
            "schema": publication.INTENT,
            "attempt": attempt,
            "plan_identity": plan_id.uri,
            "origin_identity": origin_id.uri,
            "scratch_root": str(self.scratch.path),
            "recovered_from": None,
        }
        values = {f"q/a/{attempt}/intent.json": canonical_json_bytes(intent)}

        class Files:
            def read(self, name, *, max_bytes):
                if name not in values:
                    raise FileNotFoundError(name)
                content = values[name]
                if len(content) > max_bytes:
                    raise ValueError("fixture bounded read")
                return content

        return current, attempt, intent, values, Files()

    def test_missing_path_without_original_owner_completion_does_not_allow_replay(self):
        self.scratch.retire()
        current, attempt, _intent, _values, files = self.records()
        with patch.object(publication, "_project_roots", return_value=()):
            with self.assertRaisesRegex(ValueError, "run_resources_not_retired"):
                publication._retired(files, current, attempt)

    def test_retirement_record_is_bound_to_actual_intent_attempt_and_root(self):
        identity = self.scratch.retire()
        current, attempt, intent, values, files = self.records()
        record = {
            "schema": publication.RETIREMENT,
            "attempt": attempt,
            "intent_identity": canonical_identity(intent).to_dict(),
            "scratch_root": str(self.scratch.path),
            "identity": identity,
        }
        name = f"q/a/{attempt}/retirement.json"
        with patch.object(publication, "_project_roots", return_value=()):
            values[name] = canonical_json_bytes(record)
            self.assertEqual(publication._retired(files, current, attempt), intent)
            for field, value in (
                ("attempt", "b" * 32),
                ("intent_identity", canonical_identity("different").to_dict()),
                ("scratch_root", str(self.parent / "foreign")),
                ("identity", [True, 3]),
            ):
                with self.subTest(field=field):
                    values[name] = canonical_json_bytes({**record, field: value})
                    with self.assertRaisesRegex(
                        ValueError, "retirement_record_invalid"
                    ):
                        publication._retired(files, current, attempt)
            values[name] = canonical_json_bytes(record)
            self.scratch.path.mkdir()
            (self.scratch.path / "foreign").write_text("foreign")
            with self.assertRaisesRegex(ValueError, "run_resources_not_retired"):
                publication._retired(files, current, attempt)
