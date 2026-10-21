"""Faithful retained topology across actual capture, mutation and retirement."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import retained_project_inputs as inputs
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
    RetainedProjectTreePolicy,
)


class RetainedProjectTreeContractTests(unittest.TestCase):
    def setUp(self):
        self.limits = RetainedProjectLimits(30, 1024, 4096)
        self.file = RetainedProjectMember(
            "sdk/CC", 1, 0o755, hashlib.sha256(b"x").hexdigest(), "sdk"
        )
        self.alias = replace(self.file, path="sdk/cc", hardlink_target="sdk/CC")

    def manifest(self, **kwargs):
        return RetainedProjectManifest(
            ("sdk",), (self.file, self.alias), self.limits, ("sdk",), **kwargs
        )

    def test_explicit_policy_roundtrip_binds_names_links_and_role(self):
        manifest = self.manifest(tree_policy=RetainedProjectTreePolicy(True))
        self.assertEqual(
            RetainedProjectManifest.from_dict(manifest.to_dict()), manifest
        )
        ordinary = replace(self.alias, path="sdk/compiler", hardlink_target=None)
        portable = replace(manifest, members=(self.file, ordinary), tree_policy=None)
        selected = replace(portable, tree_policy=RetainedProjectTreePolicy())
        self.assertNotEqual(portable.identity, selected.identity)
        self.assertNotEqual(
            portable.role_identity({"sdk"}), selected.role_identity({"sdk"})
        )
        self.assertNotEqual(
            manifest.identity,
            replace(
                manifest, members=(self.file, replace(self.alias, hardlink_target=None))
            ).identity,
        )

    def test_default_collision_and_unselected_hardlinks_refuse(self):
        with self.assertRaises(ValueError):
            self.manifest()
        with self.assertRaises(ValueError):
            self.manifest(tree_policy=RetainedProjectTreePolicy())
        with self.assertRaisesRegex(ValueError, "canonical"):
            replace(
                self.manifest(tree_policy=RetainedProjectTreePolicy(True)),
                members=(self.file, replace(self.alias, path="sdk/compiler")),
                tree_policy=None,
            )

    def test_invalid_link_targets_and_policy_refuse(self):
        for target in ("sdk/cc", "../escape", "sdk/zero"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                replace(
                    self.manifest(tree_policy=RetainedProjectTreePolicy(True)),
                    members=(self.file, replace(self.alias, hardlink_target=target)),
                )
        with self.assertRaises(ValueError):
            replace(
                self.manifest(tree_policy=RetainedProjectTreePolicy(True)),
                members=(self.file, replace(self.alias, sha256="0" * 64)),
            )
        for value in (
            None,
            {},
            {"schema": "future", "case_sensitive": True},
            {
                "schema": "literate-ai/retained-project-tree-policy@1",
                "case_sensitive": 1,
            },
        ):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                RetainedProjectTreePolicy.from_dict(value)

    def test_legacy_wire_and_role_identity_remain_exact(self):
        manifest = RetainedProjectManifest(
            ("sdk",), (self.file,), self.limits, ("sdk",)
        )
        member = {
            "path": "sdk/CC",
            "size": 1,
            "mode": 0o755,
            "sha256": self.file.sha256,
            "role": "sdk",
        }
        wire = {
            "schema": "literate-ai/retained-project-manifest@1",
            "roots": ["sdk"],
            "directories": ["sdk"],
            "members": [member],
            "limits": self.limits.to_dict(),
        }
        self.assertEqual(manifest.to_dict(), wire)
        self.assertEqual(manifest.identity, canonical_identity(wire))
        self.assertEqual(
            manifest.role_identity({"sdk"}),
            canonical_identity(
                {
                    "schema": "literate-ai/retained-role-closure@1",
                    "roles": ["sdk"],
                    "members": [member],
                }
            ),
        )
        for field in ("symlink_target", "hardlink_target"):
            with self.assertRaises(ValueError):
                RetainedProjectMember.from_dict({**member, field: None})


@unittest.skipIf(os.name == "nt", "Faithful retained topology requires POSIX")
class RetainedProjectTreeInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="retained-tree-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        (self.source / "sdk/bin").mkdir(parents=True)
        (self.source / "sdk/bin/compiler").write_bytes(b"compiler")
        (self.source / "sdk/bin/compiler").chmod(0o755)
        os.link(self.source / "sdk/bin/compiler", self.source / "sdk/bin/cc")
        (self.source / "sdk/public").symlink_to("bin", target_is_directory=True)
        self.limits = RetainedProjectLimits(30, 1024, 4096)

    def discover(self, **kwargs):
        return inputs.discover_retained_project_inputs(
            self.source,
            {"sdk": "sdk"},
            self.limits,
            allow_internal_symlinks=True,
            tree_policy=RetainedProjectTreePolicy(**kwargs),
        )

    def test_capture_preserves_links_after_source_retirement_without_cas_inode_sharing(
        self,
    ):
        manifest = self.discover()
        placed = inputs.capture_retained_project_inputs(
            self.source, manifest, self.base / "snapshot"
        )
        shutil.rmtree(self.source)
        inputs.verify_retained_project_inputs(placed, manifest)
        representative = placed / "sdk/bin/cc"
        self.assertEqual(
            representative.stat().st_ino, (placed / "sdk/bin/compiler").stat().st_ino
        )
        digest = hashlib.sha256(b"compiler").hexdigest()
        self.assertNotEqual(
            representative.stat().st_ino,
            (self.base / "snapshot/blobs" / digest).stat().st_ino,
        )
        self.assertEqual((placed / "sdk/public/compiler").read_bytes(), b"compiler")

    def test_same_bytes_independent_replacement_refuses(self):
        manifest = self.discover()
        path = self.source / "sdk/bin/compiler"
        path.unlink()
        path.write_bytes(b"compiler")
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "topology"):
            inputs.verify_retained_project_inputs(self.source, manifest)

    def test_alias_names_still_charge_declared_aggregate_bounds(self):
        self.limits = replace(self.limits, max_total_bytes=12)
        with self.assertRaisesRegex(ValueError, "bound"):
            self.discover()

    def test_external_hardlink_refuses_and_keeps_external_file(self):
        outside = self.base / "outside"
        os.link(self.source / "sdk/bin/cc", outside)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.discover()
        self.assertEqual(outside.read_bytes(), b"compiler")

    def test_replacement_during_discovery_refuses(self):
        original = inputs._stream
        changed = False

        def mutate(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                changed = True
                path = self.source / "sdk/bin/compiler"
                path.unlink()
                path.write_bytes(b"compiler")
                path.chmod(0o755)
            return result

        with mock.patch.object(inputs, "_stream", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.discover()

    def test_link_creation_failure_cleans_only_owned_staging(self):
        manifest = self.discover()
        sibling = self.base / "keep"
        sibling.write_bytes(b"sentinel")
        with mock.patch.object(inputs.os, "link", side_effect=OSError("link failed")):
            with self.assertRaisesRegex(OSError, "link failed"):
                inputs.capture_retained_project_inputs(
                    self.source, manifest, self.base / "snapshot"
                )
        self.assertFalse((self.base / "snapshot").exists())
        self.assertEqual(list(self.base.glob(".retained-*")), [])
        self.assertEqual(sibling.read_bytes(), b"sentinel")

    def test_case_sensitive_selection_checks_real_filesystem(self):
        manifest = self.discover(case_sensitive=True)
        probe = self.base / "Aa"
        probe.write_bytes(b"")
        supports_case = not (self.base / "aa").exists()
        probe.unlink()
        if supports_case:
            upper = self.source / "sdk/bin/CC"
            os.link(self.source / "sdk/bin/cc", upper)
            manifest = self.discover(case_sensitive=True)
            placed = inputs.capture_retained_project_inputs(
                self.source, manifest, self.base / "snapshot"
            )
            self.assertEqual(
                (placed / "sdk/bin/CC").stat().st_ino,
                (placed / "sdk/bin/cc").stat().st_ino,
            )
        else:
            with self.assertRaisesRegex(ValueError, "case-sensitive"):
                inputs.capture_retained_project_inputs(
                    self.source, manifest, self.base / "snapshot"
                )
            self.assertFalse((self.base / "snapshot").exists())


if __name__ == "__main__":
    unittest.main()
