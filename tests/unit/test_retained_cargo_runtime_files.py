"""Runtime search directories retain bytes, names and physical replacements."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.retained_cargo_runtime_files import (
    capture_retained_cargo_runtime_files,
)


class RetainedCargoRuntimeFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "library"
        self.file.write_bytes(b"runtime bytes")

    def test_exact_capture_and_stable_identity(self):
        captured = capture_retained_cargo_runtime_files((self.root, self.root))
        captured.require_unchanged()
        self.assertEqual(captured.roots, (self.root,))
        self.assertEqual(captured.closure.total_bytes, len(b"runtime bytes"))
        self.assertEqual(
            captured.identity,
            capture_retained_cargo_runtime_files((self.root,)).identity,
        )

    def test_mutation_addition_and_same_bytes_replacement_refuse(self):
        for change in ("bytes", "addition", "replacement"):
            with self.subTest(change=change):
                captured = capture_retained_cargo_runtime_files((self.root,))
                if change == "bytes":
                    self.file.write_bytes(b"changed")
                elif change == "addition":
                    (self.root / "new-library").write_bytes(b"new")
                else:
                    content = self.file.read_bytes()
                    self.file.unlink()
                    self.file.write_bytes(content)
                with self.assertRaises(ValueError):
                    captured.require_unchanged()

    def test_symlinked_library_and_unsafe_roots_refuse(self):
        link = self.root / "alias"
        try:
            link.symlink_to(self.file)
        except OSError:
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            capture_retained_cargo_runtime_files((self.root,))
        link.unlink()
        for roots in ((), (Path("relative"),), (self.root / "..",)):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                capture_retained_cargo_runtime_files(roots)

    def alias_fixture(self):
        directory = self.root / "compiler"
        directory.mkdir()
        alias = directory / "runtime"
        try:
            alias.symlink_to("../library")
        except OSError:
            self.skipTest("host does not permit symbolic links")
        return directory, alias

    def test_explicit_compiler_alias_captures_external_target_bytes(self):
        directory, alias = self.alias_fixture()
        captured = capture_retained_cargo_runtime_files(
            (directory,), file_alias_roots=(directory,)
        )
        captured.require_unchanged()
        self.assertEqual(captured.closure.total_bytes, len(b"runtime bytes"))
        self.assertEqual(captured.aliases[0][:2], (alias, self.file))
        self.assertEqual(
            captured.identity,
            capture_retained_cargo_runtime_files(
                (directory,), file_alias_roots=(directory,)
            ).identity,
        )
        self.file.write_bytes(b"changed target")
        with self.assertRaises(ValueError):
            captured.require_unchanged()

    def test_alias_retarget_and_same_target_replacement_refuse(self):
        directory, alias = self.alias_fixture()
        other = self.root / "other"
        other.write_bytes(self.file.read_bytes())
        for target in ("../library", "../other"):
            with self.subTest(target=target):
                captured = capture_retained_cargo_runtime_files(
                    (directory,), file_alias_roots=(directory,)
                )
                alias.unlink()
                alias.symlink_to(target)
                with self.assertRaises(ValueError):
                    captured.require_unchanged()

    def test_alias_target_same_bytes_replacement_refuses(self):
        directory, _ = self.alias_fixture()
        captured = capture_retained_cargo_runtime_files(
            (directory,), file_alias_roots=(directory,)
        )
        content = self.file.read_bytes()
        self.file.unlink()
        self.file.write_bytes(content)
        with self.assertRaises(ValueError):
            captured.require_unchanged()

    def test_only_selected_compiler_root_allows_aliases_in_mixed_capture(self):
        directory, _ = self.alias_fixture()
        generated = self.root / "generated"
        generated.mkdir()
        (generated / "output").write_bytes(b"generated")
        captured = capture_retained_cargo_runtime_files(
            (generated, directory), file_alias_roots=(directory,)
        )
        captured.require_unchanged()
        (generated / "alias").symlink_to("../library")
        with self.assertRaises(ValueError):
            captured.require_unchanged()
        with self.assertRaises(ValueError):
            capture_retained_cargo_runtime_files(
                (generated, directory), file_alias_roots=(directory,)
            )
        with self.assertRaises(ValueError):
            capture_retained_cargo_runtime_files(
                (generated,), file_alias_roots=(directory,)
            )

    def test_file_alias_chain_is_bound_and_cycles_refuse(self):
        directory, alias = self.alias_fixture()
        middle = self.root / "middle"
        middle.symlink_to("library")
        alias.unlink()
        alias.symlink_to("../middle")
        captured = capture_retained_cargo_runtime_files(
            (directory,), file_alias_roots=(directory,)
        )
        self.assertEqual(len(captured.aliases[0][2]), 2)
        middle.unlink()
        middle.symlink_to("middle")
        with self.assertRaises(ValueError):
            captured.require_unchanged()
        with self.assertRaises(ValueError):
            capture_retained_cargo_runtime_files(
                (directory,), file_alias_roots=(directory,)
            )

    def test_directory_dangling_and_interior_traversal_aliases_refuse(self):
        directory, alias = self.alias_fixture()
        for target in ("..", "../missing", "../compiler/../library"):
            with self.subTest(target=target):
                alias.unlink()
                alias.symlink_to(target)
                with self.assertRaises((ValueError, OSError)):
                    capture_retained_cargo_runtime_files(
                        (directory,), file_alias_roots=(directory,)
                    )

    def test_alias_parent_replacement_refuses(self):
        directory, alias = self.alias_fixture()
        external = self.root / "external"
        external.mkdir()
        target = external / "runtime"
        target.write_bytes(b"bytes")
        alias.unlink()
        alias.symlink_to("../external/runtime")
        captured = capture_retained_cargo_runtime_files(
            (directory,), file_alias_roots=(directory,)
        )
        external.rename(self.root / "old-external")
        external.mkdir()
        (self.root / "old-external/runtime").rename(target)
        with self.assertRaises(ValueError):
            captured.require_unchanged()

    def test_alias_chain_limit_and_invalid_root_policy_refuse(self):
        directory, alias = self.alias_fixture()
        alias.unlink()
        alias.symlink_to("../link-0")
        for index in range(64):
            (self.root / f"link-{index}").symlink_to(
                f"link-{index + 1}" if index < 63 else "library"
            )
        with self.assertRaisesRegex(ValueError, "alias-unsafe"):
            capture_retained_cargo_runtime_files(
                (directory,), file_alias_roots=(directory,)
            )
        for invalid in (1, "true", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                capture_retained_cargo_runtime_files(
                    (directory,), file_alias_roots=invalid
                )
