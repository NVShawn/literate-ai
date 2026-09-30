"""ELF path imports must not fall back to directory or cache lookup."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.dependencies.observation import (
    DependencyObservationError,
    LinuxElfDependencyObserver,
    _ElfImage,
)


class ElfDirectImportTests(unittest.TestCase):
    def observe(self, root: Path, needed: str):
        app = str(root / "app")
        library = str(root / "lib" / "provider.so")
        images = {
            app: _ElfImage(app, "app", ("ELF64", "fixture"), (needed,), (), None),
            library: _ElfImage(library, "library", ("ELF64", "fixture"), (), (), None),
        }
        observer = LinuxElfDependencyObserver(library_roots=(root / "fallback",))
        with (
            mock.patch(
                "literate_ai.adapters.dependencies.observation._inspect_elf",
                side_effect=lambda _tool, path: images[str(path)],
            ),
            mock.patch(
                "literate_ai.adapters.dependencies.observation._resolve_exact_elf_candidate",
                return_value=library,
            ) as resolve,
        ):
            result = observer._closure(
                root / "readelf",
                {app: {"runtime"}},
                {needed: (str(root / "cache" / "wrong.so"),)},
            )
        return result, resolve

    def test_absolute_import_bypasses_cache_and_library_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = str(root / "lib" / "provider.so")
            result, resolve = self.observe(root, library)
            self.assertEqual(resolve.call_args.args[0], (library,))
            self.assertEqual(result[1], ((str(root / "app"), library),))

    def test_origin_import_bypasses_cache_and_library_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for token in ("$ORIGIN", "${ORIGIN}"):
                with self.subTest(token=token):
                    _, resolve = self.observe(root, token + "/lib/provider.so")
                    self.assertEqual(
                        resolve.call_args.args[0],
                        (str(root / "lib" / "provider.so"),),
                    )

    def test_relative_import_requires_unmodeled_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(DependencyObservationError) as caught:
                self.observe(Path(temporary), "lib/provider.so")
            self.assertEqual(
                caught.exception.code, "dependencies.elf-import-path-unsafe"
            )

    def test_unknown_token_cannot_be_treated_as_a_literal_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                "$LIB/provider.so",
                "${PLATFORM}/provider.so",
                "$ORIGIN_SUFFIX/provider.so",
            ):
                with self.subTest(name=name):
                    with self.assertRaises(DependencyObservationError) as caught:
                        self.observe(root, name)
                    self.assertEqual(
                        caught.exception.code,
                        "dependencies.elf-import-token-unsupported",
                    )


if __name__ == "__main__":
    unittest.main()
