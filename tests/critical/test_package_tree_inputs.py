"""Package tree authority and real relocation without flattening link topology."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.native_sdk_tree_custody import capture_native_sdk_tree
from literate_ai.adapters.package_tree_inputs import (
    materialize_package_tree_input,
    verify_package_tree_input,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.executable_components.packages import PackageInput
from literate_ai.contracts.package_tree_inputs import (
    PackageTreeInput,
    PackageTreeKind,
    require_package_tree_namespace,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.storage.cas import FileSystemCAS


@unittest.skipIf(os.name == "nt", "Faithful package tree custody requires POSIX")
class PackageTreeInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="package-tree-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        (self.source / "bin").mkdir(parents=True)
        (self.source / "empty").mkdir()
        (self.source / "bin/cc").write_bytes(b"compiler")
        (self.source / "bin/cc").chmod(0o755)
        os.link(self.source / "bin/cc", self.source / "bin/compiler")
        (self.source / "public").symlink_to("bin", target_is_directory=True)
        self.store = FileSystemCAS(self.base / "store")
        self.parent = self.base / "staging"
        self.parent.mkdir()
        self.limits = RetainedProjectLimits(30, 1024, 4096)
        tree = capture_native_sdk_tree(
            self.source, store=self.store, limits=self.limits
        )
        self.value = PackageTreeInput(
            "native-sdks/sdk",
            "native-sdk-tree",
            canonical_identity("source"),
            canonical_identity("target"),
            tree,
        )

    def materialize(self, **kwargs):
        return materialize_package_tree_input(
            self.value,
            expected_identity=self.value.identity,
            store=self.store,
            limits=self.limits,
            parent=self.parent,
            **kwargs,
        )

    def verify(self, root):
        verify_package_tree_input(
            root,
            self.value,
            expected_identity=self.value.identity,
            store=self.store,
            limits=self.limits,
        )

    def test_roundtrip_binds_provenance_namespace_naming_and_exact_alias_text(self):
        self.assertEqual(PackageTreeInput.from_dict(self.value.to_dict()), self.value)
        self.assertNotEqual(
            replace(self.value, kind=PackageTreeKind.ARTIFACT).identity,
            self.value.identity,
        )
        with self.assertRaises(TypeError):
            replace(self.value, kind="resource")
        for field, value in (
            ("prefix", "other"),
            ("role", "other-role"),
            ("source_identity", canonical_identity("other source")),
            ("target_identity", canonical_identity("other target")),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(
                    replace(self.value, **{field: value}).identity, self.value.identity
                )
        aliases = tuple(
            replace(alias, target="./bin") if alias.path == "public" else alias
            for alias in self.value.tree.aliases
        )
        self.assertNotEqual(
            replace(
                self.value, tree=replace(self.value.tree, aliases=aliases)
            ).identity,
            self.value.identity,
        )
        self.assertNotEqual(
            replace(
                self.value, tree=replace(self.value.tree, case_sensitive=True)
            ).identity,
            self.value.identity,
        )
        with self.assertRaises(ValueError):
            PackageInput.from_dict(self.value.to_dict())
        for mutation in ({"schema": "future"}, {"accepted": True}):
            with self.assertRaises(ValueError):
                PackageTreeInput.from_dict({**self.value.to_dict(), **mutation})

    def test_namespace_rejects_overlaps_aliases_and_target_substitution(self):
        require_package_tree_namespace(
            (self.value,), ("bin/app",), target_identity=self.value.target_identity
        )
        for paths, trees in (
            (("native-sdks/sdk/member",), (self.value,)),
            (("native-sdks",), (self.value,)),
            (("NATIVE-SDKS/SDK",), (self.value,)),
            ((), (self.value, replace(self.value, prefix="native-sdks/sdk/child"))),
            ((), (self.value, self.value)),
        ):
            with (
                self.subTest(paths=paths),
                self.assertRaisesRegex(ValueError, "ownership"),
            ):
                require_package_tree_namespace(
                    trees, paths, target_identity=self.value.target_identity
                )
        with self.assertRaisesRegex(ValueError, "ownership"):
            require_package_tree_namespace(
                (), ("A/x", "a/y"), target_identity=self.value.target_identity
            )
        with self.assertRaisesRegex(ValueError, "target"):
            require_package_tree_namespace(
                (self.value,), (), target_identity=canonical_identity("wrong target")
            )
        selected = replace(
            self.value, tree=replace(self.value.tree, case_sensitive=True)
        )
        with self.assertRaisesRegex(ValueError, "case-sensitive"):
            require_package_tree_namespace(
                (selected,), (), target_identity=selected.target_identity
            )
        require_package_tree_namespace(
            (selected,),
            ("Case", "case"),
            target_identity=selected.target_identity,
            case_sensitive=True,
        )

    def test_relocation_preserves_links_empty_directories_and_modes_after_retirement(
        self,
    ):
        shutil.rmtree(self.source)
        placed = self.materialize()
        self.verify(placed)
        self.assertEqual((placed / "public/compiler").read_bytes(), b"compiler")
        self.assertEqual(os.readlink(placed / "public"), "bin")
        self.assertEqual(
            (placed / "bin/cc").stat().st_ino, (placed / "bin/compiler").stat().st_ino
        )
        self.assertTrue((placed / "empty").is_dir())
        self.assertTrue((placed / "bin/cc").stat().st_mode & 0o111)

    def test_identical_bytes_hardlink_replacement_is_not_the_same_input(self):
        placed = self.materialize()
        path = placed / "bin/compiler"
        path.unlink()
        path.write_bytes(b"compiler")
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "topology"):
            self.verify(placed)

    def test_expected_identity_and_bounds_refuse_before_publication(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            materialize_package_tree_input(
                self.value,
                expected_identity=canonical_identity("forged"),
                store=self.store,
                limits=self.limits,
                parent=self.parent,
            )
        with self.assertRaises(ValueError):
            materialize_package_tree_input(
                self.value,
                expected_identity=self.value.identity,
                store=self.store,
                limits=RetainedProjectLimits(1, 1, 1),
                parent=self.parent,
            )
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assertEqual((self.source / "bin/cc").read_bytes(), b"compiler")


if __name__ == "__main__":
    unittest.main()
