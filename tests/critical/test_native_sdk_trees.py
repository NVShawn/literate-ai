"""SDK alias resolution preserves inventoried paths and refuses unsafe graphs."""

from __future__ import annotations

import dataclasses
import hashlib
import unittest

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias, NativeSdkTree
from literate_ai.contracts.native_sdks import NativeSdkFile
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def blob(value: bytes) -> BlobRef:
    return BlobRef(hashlib.sha256(value).hexdigest(), len(value))


class NativeSdkTreeTests(unittest.TestCase):
    def tree(self, *aliases: tuple[str, str]) -> NativeSdkTree:
        return NativeSdkTree(
            ("bin", "include", "include/sys", "lib"),
            (
                NativeSdkFile("bin/llvm-ar", blob(b"compiler"), True),
                NativeSdkFile("include/sys/errno.h", blob(b"header")),
                NativeSdkFile("lib/libc.so.7", blob(b"library")),
            ),
            tuple(NativeSdkAlias(*item) for item in sorted(aliases)),
        )

    def test_compiler_header_library_and_directory_aliases(self) -> None:
        tree = self.tree(
            ("bin/ar", "llvm-ar"),
            ("headers", "include"),
            ("include/errno.h", "sys/errno.h"),
            ("lib/libc.so", "libc.so.7"),
            ("system", "headers/sys"),
        )
        for logical, physical in (
            ("bin/ar", "bin/llvm-ar"),
            ("headers/errno.h", "include/sys/errno.h"),
            ("system/errno.h", "include/sys/errno.h"),
            ("lib/libc.so", "lib/libc.so.7"),
        ):
            with self.subTest(logical=logical):
                self.assertEqual(tree.resolve(logical), physical)
        SchemaCatalog().validate(tree.SCHEMA, tree.to_dict())
        self.assertEqual(NativeSdkTree.from_dict(tree.to_dict()), tree)

    def test_parent_traversal_uses_resolved_directory(self) -> None:
        tree = self.tree(
            ("headers", "include/sys"),
            ("public", "headers/../sys/errno.h"),
        )
        self.assertEqual(tree.resolve("public"), "include/sys/errno.h")
        self.assertEqual(tree.aliases[1].target, "headers/../sys/errno.h")

    def test_repeated_alias_use_in_a_finite_path_is_valid(self) -> None:
        tree = self.tree(("headers", "include"), ("public", "headers/../headers/sys"))
        self.assertEqual(tree.resolve("public/errno.h"), "include/sys/errno.h")

    def test_escape_dangling_cycles_and_file_traversal_refuse(self) -> None:
        for aliases in (
            (("bad", "../outside"),),
            (("bad", "include/../../outside"),),
            (("bad", "missing/../include"),),
            (("bad", "bin/llvm-ar/../include"),),
            (("bad", "bin/llvm-ar/."),),
            (("bad", "bad"),),
            (("bad", "other"), ("other", "bad")),
            (("bad", "bad/child"),),
        ):
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                self.tree(*aliases)

    def test_absolute_and_nonportable_target_text_refuses(self) -> None:
        for target in ("/etc/passwd", "C:/temp", "a\\b", "a//b", "a/", "a\x00b"):
            with (
                self.subTest(target=target),
                self.assertRaises((TypeError, ValueError)),
            ):
                NativeSdkAlias("bad", target)

    def test_physical_ownership_and_ancestor_ambiguity_refuse(self) -> None:
        base = self.tree()
        for values in (
            {"directories": ("bin", "include", "lib")},
            {"aliases": (NativeSdkAlias("BIN", "include"),)},
            {"aliases": (NativeSdkAlias("bin", "include"),)},
            {"files": base.files + (base.files[-1],)},
            {"directories": tuple(reversed(base.directories))},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                dataclasses.replace(base, **values)

    def test_exact_alias_text_and_target_bytes_change_identity(self) -> None:
        first = self.tree(("headers", "include"))
        second = self.tree(("headers", "./include"))
        self.assertEqual(first.resolve("headers"), second.resolve("headers"))
        self.assertNotEqual(first.identity, second.identity)
        changed = dataclasses.replace(
            first,
            files=(dataclasses.replace(first.files[0], blob=blob(b"changed")),)
            + first.files[1:],
        )
        self.assertNotEqual(first.identity, changed.identity)
        self.assertNotEqual(first.identity, canonical_identity("producer-admission"))

    def test_excessive_alias_expansion_refuses(self) -> None:
        aliases = tuple((f"a{index:03}", f"a{index + 1:03}") for index in range(65))
        with self.assertRaisesRegex(ValueError, "expansion limit"):
            self.tree(*aliases, ("a065", "include"))

    def test_unknown_wire_fields_and_schema_refuse(self) -> None:
        tree = self.tree()
        for data in (
            {**tree.to_dict(), "admitted": True},
            {
                **tree.to_dict(),
                "schema": "urn:literate-ai:schema:v2:native-sdk-snapshot",
            },
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                NativeSdkTree.from_dict(data)

    def test_hard_links_resolve_to_one_canonical_file(self) -> None:
        tree = dataclasses.replace(
            self.tree(),
            aliases=(
                NativeSdkAlias("bin/z-ar", "bin/llvm-ar", "hardlink"),
                NativeSdkAlias("public", "bin/z-ar"),
            ),
        )
        self.assertEqual(tree.resolve("bin/z-ar"), "bin/llvm-ar")
        self.assertEqual(tree.resolve("public"), "bin/llvm-ar")
        self.assertEqual(tree.aliases[0].target, "bin/llvm-ar")
        self.assertEqual(NativeSdkTree.from_dict(tree.to_dict()), tree)
        SchemaCatalog().validate(tree.SCHEMA, tree.to_dict())
        with self.assertRaisesRegex(ValueError, "non-directory"):
            tree.resolve("public/child")

    def test_hard_links_refuse_noncanonical_or_nonphysical_ownership(self) -> None:
        for target in ("include", "missing", "bin/z-first"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                dataclasses.replace(
                    self.tree(),
                    aliases=(NativeSdkAlias("z-last", target, "hardlink"),),
                )
        for target in (
            "../bin/llvm-ar",
            "/bin/llvm-ar",
            "bin/../bin/llvm-ar",
            "z-last",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                NativeSdkAlias("z-last", target, "hardlink")
        with self.assertRaises(ValueError):
            dataclasses.replace(
                self.tree(),
                aliases=(
                    NativeSdkAlias("bin/z-first", "bin/llvm-ar", "hardlink"),
                    NativeSdkAlias("bin/z-last", "bin/z-first", "hardlink"),
                ),
            )

    def test_case_sensitive_names_require_explicit_authority(self) -> None:
        file = NativeSdkFile("bin/CC", blob(b"compiler"), True)
        aliases = (NativeSdkAlias("bin/cc", "bin/CC", "hardlink"),)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            NativeSdkTree(("bin",), (file,), aliases)
        tree = NativeSdkTree(("bin",), (file,), aliases, case_sensitive=True)
        self.assertEqual(tree.resolve("bin/cc"), "bin/CC")
        self.assertEqual(tree.resolve("bin/CC"), "bin/CC")
        with self.assertRaises(ValueError):
            tree.resolve("bin/Cc")
        self.assertEqual(NativeSdkTree.from_dict(tree.to_dict()), tree)
        SchemaCatalog().validate(tree.SCHEMA, tree.to_dict())
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                dataclasses.replace(tree, case_sensitive=value)

    def test_case_selection_changes_identity_even_without_collisions(self) -> None:
        tree = self.tree()
        self.assertNotEqual(
            tree.identity, dataclasses.replace(tree, case_sensitive=True).identity
        )

    def test_validation_budget_is_shared_across_aliases(self) -> None:
        # Each link is finite and below the per-link expansion limit, but the
        # combined graph must not turn an admitted inventory into unbounded work.
        aliases = tuple(
            (f"a{index:05}", "./" * 80 + "include") for index in range(13000)
        )
        with self.assertRaisesRegex(ValueError, "work limit"):
            self.tree(*aliases)
