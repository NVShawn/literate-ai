"""Explicit internal POSIX links retain target text without following inputs."""

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_inputs as inputs
from literate_ai.contracts.retained_component_ownership import RetainedComponentOwner
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
)
from tests.support.fixtures_test_component_lock_contracts import component_authoring
from tests.support.symlinks import skip_unless_symlinks_followable


def link(path, target):
    data = target.encode("utf-8")
    return RetainedProjectMember(
        path, len(data), 0o120000, hashlib.sha256(data).hexdigest(), "source", target
    )


class RetainedSymlinkContractTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("LITAI_RETAINED_SOURCE_CORPUS"),
        "requires an explicitly selected Git source corpus",
    )
    def test_actual_selected_git_source_corpus(self):
        source = Path(os.environ["LITAI_RETAINED_SOURCE_CORPUS"]).resolve(strict=True)

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(source), *args], timeout=60
            )

        revision = git("rev-parse", "HEAD").decode().strip()
        git("diff", "--exit-code", "HEAD", "--")
        self.assertTrue(
            all(
                row.startswith(b"H ")
                for row in git("ls-files", "-v", "-z").split(b"\0")
                if row
            )
        )
        entries = {}
        for row in git("ls-tree", "-rz", revision).split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            self.assertEqual(kind, "blob")
            entries[name.decode()] = (mode, oid)
        roots = {name.partition("/")[0]: "source" for name in entries}
        manifest = inputs.discover_retained_project_inputs(
            source,
            roots,
            RetainedProjectLimits(500_000, 1024**3, 20 * 1024**3),
            allow_internal_symlinks=True,
        )
        self.assertEqual({m.path for m in manifest.members}, set(entries))
        links = []
        for member in manifest.members:
            mode, oid = entries[member.path]
            expected_mode = {"100644": 0o644, "100755": 0o755, "120000": 0o120000}[mode]
            self.assertEqual(member.mode, expected_mode)
            if mode == "120000":
                links.append((member, oid))
        self.assertTrue(links)
        batch = subprocess.run(
            ["git", "-C", str(source), "cat-file", "--batch"],
            input="".join(oid + "\n" for _, oid in links).encode(),
            capture_output=True,
            timeout=60,
            check=True,
        )
        stream = io.BytesIO(batch.stdout)
        for member, _ in links:
            size = int(stream.readline().split()[2])
            self.assertEqual(member.symlink_target.encode("utf-8"), stream.read(size))
            self.assertEqual(stream.read(1), b"\n")
        self.assertEqual(git("rev-parse", "HEAD").decode().strip(), revision)
        git("diff", "--exit-code", "HEAD", "--")
        summary = {
            "revision": revision,
            "manifest_identity": manifest.identity.uri,
            "members": len(manifest.members),
            "directories": len(manifest.directories),
            "symlinks": len(links),
            "tracked_paths_modes_and_link_targets_match": True,
            "acceptance": "input-custody-only",
        }
        report = os.environ.get("LITAI_RETAINED_SOURCE_CORPUS_REPORT")
        if report:
            target = Path(report).absolute()
            self.assertFalse(target == source or source in target.parents)
            with target.open("x") as output:
                json.dump({"summary": summary, "manifest": manifest.to_dict()}, output)
                output.write("\n")
        print(json.dumps(summary))

    def test_legacy_member_wire_stays_unchanged(self):
        member = RetainedProjectMember("src/main.c", 0, 0o644, "0" * 64, "source")
        self.assertEqual(
            set(member.to_dict()), {"path", "size", "mode", "sha256", "role"}
        )
        self.assertEqual(RetainedProjectMember.from_dict(member.to_dict()), member)
        with self.assertRaises(ValueError):
            RetainedProjectMember.from_dict(
                {**member.to_dict(), "symlink_target": None}
            )

    def test_link_wire_binds_exact_target_size_hash_and_kind(self):
        member = link("src/link", "../assets/actual")
        self.assertEqual(member.symlink_destination, "assets/actual")
        self.assertEqual(RetainedProjectMember.from_dict(member.to_dict()), member)
        for kwargs in ({"sha256": "0" * 64}, {"size": 0}, {"mode": 0o644}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(member, **kwargs)
        for target in (
            "",
            "/outside",
            "C:outside",
            "dir\\outside",
            "a\x00b",
            "a\nb",
            "a" * 4097,
        ):
            with self.subTest(target=target[:30]), self.assertRaises(ValueError):
                link("src/link", target)

    def test_wire_refuses_missing_targets_cycles_and_non_directory_traversal(self):
        file = RetainedProjectMember("src/main", 1, 0o644, "0" * 64, "source")
        limits = RetainedProjectLimits(100, 1000, 10000)
        variants = (
            (link("src/link", "absent"),),
            (link("src/a", "b"), link("src/b", "a")),
            (link("src/link", "main/../main"),),
            (link("src/alias", "."), link("src/link", "alias/../main")),
        )
        for links in variants:
            with self.subTest(links=links), self.assertRaises(ValueError):
                RetainedProjectManifest(
                    ("src",),
                    tuple(sorted((file, *links), key=lambda m: m.path)),
                    limits,
                    ("src",),
                )

    def test_symlink_ownership_is_bound_like_every_other_input(self):
        file = RetainedProjectMember("src/main", 1, 0o644, "0" * 64, "source")
        shared, alias = component_authoring("shared"), component_authoring("alias")
        owner = RetainedComponentOwner("src", shared.coordinate, shared.identity)
        manifest = RetainedProjectManifest(
            ("src",),
            (link("src/link", "main"), file),
            RetainedProjectLimits(100, 100, 1000),
            ("src",),
            (owner,),
        )
        changed = replace(
            manifest,
            component_ownership=(
                owner,
                RetainedComponentOwner("src/link", alias.coordinate, alias.identity),
            ),
        )
        self.assertNotEqual(manifest.identity, changed.identity)
        self.assertEqual(RetainedProjectManifest.from_dict(changed.to_dict()), changed)


@unittest.skipIf(
    os.name == "nt", "POSIX symlink custody is explicitly unsupported on Windows"
)
class RetainedSymlinkFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        skip_unless_symlinks_followable(self, self.base)
        self.root = self.base / "source"
        (self.root / "src/empty").mkdir(parents=True)
        (self.root / "assets").mkdir()
        (self.root / "assets/data").write_bytes(b"\x00\x01binary\xff")
        (self.root / "src/main.c").write_text("retained\n")
        (self.root / "src/link").symlink_to("../assets/data")
        self.roots = {"src": "source", "assets": "asset"}
        self.limits = RetainedProjectLimits(100, 1000, 10000)

    def discover(self):
        return inputs.discover_retained_project_inputs(
            self.root, self.roots, self.limits, allow_internal_symlinks=True
        )

    def test_default_discovery_still_refuses_internal_links(self):
        with self.assertRaises(ValueError):
            inputs.discover_retained_project_inputs(self.root, self.roots, self.limits)
        with self.assertRaises(TypeError):
            inputs.discover_retained_project_inputs(
                self.root, self.roots, self.limits, allow_internal_symlinks=1
            )

    def test_capture_preserves_links_target_bytes_and_read_only_file_modes(self):
        manifest = self.discover()
        tree = inputs.capture_retained_project_inputs(
            self.root, manifest, self.base / "snapshot"
        )
        self.assertTrue((tree / "src/link").is_symlink())
        self.assertEqual(os.readlink(tree / "src/link"), "../assets/data")
        self.assertEqual((tree / "src/link").read_bytes(), b"\x00\x01binary\xff")
        member = next(m for m in manifest.members if m.path == "src/link")
        self.assertEqual(
            (tree.parent / "blobs" / member.sha256).read_bytes(), b"../assets/data"
        )
        self.assertFalse((tree / "assets/data").stat().st_mode & 0o222)
        inputs.verify_retained_project_inputs(tree, manifest)

    def test_directory_and_final_link_chains_preserved_without_inventory_recursion(
        self,
    ):
        (self.root / "src/dir").symlink_to("empty", target_is_directory=True)
        (self.root / "src/again").symlink_to("dir", target_is_directory=True)
        (self.root / "src/self-parent").symlink_to(".", target_is_directory=True)
        manifest = self.discover()
        self.assertEqual(manifest.directories, ("assets", "src", "src/empty"))
        tree = inputs.capture_retained_project_inputs(
            self.root, manifest, self.base / "snapshot"
        )
        self.assertTrue((tree / "src/again").is_symlink())
        self.assertTrue((tree / "src/again").is_dir())
        self.assertEqual(os.readlink(tree / "src/self-parent"), ".")

    def test_external_and_dangling_targets_refuse(self):
        path = self.root / "src/link"
        for target in ("../../outside", str(self.base / "outside"), "missing"):
            path.unlink()
            path.symlink_to(target)
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.discover()

    def test_target_change_invalidates_even_when_file_contents_are_equal(self):
        (self.root / "assets/other").write_bytes(
            (self.root / "assets/data").read_bytes()
        )
        manifest = self.discover()
        tree = inputs.capture_retained_project_inputs(
            self.root, manifest, self.base / "snapshot"
        )
        path = self.root / "src/link"
        path.unlink()
        path.symlink_to("../assets/other")
        with self.assertRaises(ValueError):
            inputs.verify_retained_project_inputs(self.root, manifest)
        inputs.verify_retained_project_inputs(tree, manifest)
        path.unlink()
        path.write_bytes(b"../assets/data")
        with self.assertRaises(ValueError):
            inputs.verify_retained_project_inputs(self.root, manifest)

    def test_link_mutation_during_read_refuses_without_publishing(self):
        manifest = self.discover()
        original = os.readlink
        path = self.root / "src/link"

        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            path.unlink()
            path.symlink_to("../assets/data")
            return result

        with patch.object(inputs.os, "readlink", side_effect=mutate):
            with self.assertRaises(ValueError):
                inputs.capture_retained_project_inputs(
                    self.root, manifest, self.base / "snapshot"
                )
        self.assertFalse((self.base / "snapshot").exists())

    def test_link_target_text_counts_toward_byte_budget(self):
        with self.assertRaises(ValueError):
            inputs.discover_retained_project_inputs(
                self.root,
                self.roots,
                RetainedProjectLimits(100, 10, 10000),
                allow_internal_symlinks=True,
            )
