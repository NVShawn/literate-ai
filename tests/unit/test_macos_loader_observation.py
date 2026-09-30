"""Explicit application paths must not retarget inspector dependencies."""

import unittest
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.dependencies.observation import (
    DependencyObservationError,
    MacOsMachODependencyObserver,
)
from literate_ai.adapters.macos_loader_paths import MacOsLoaderPaths


class FixtureObserver(MacOsMachODependencyObserver):
    def __init__(self, images, paths):
        super().__init__(loader_paths=paths)
        self.images = images

    def _inspect(self, path):
        return SimpleNamespace(path=path, rpaths=(), linked_paths=self.images[path])

    def _run_dyld(self, arguments):
        if arguments[1] not in self.images:
            raise DependencyObservationError("fixture.missing", "missing image")
        return ""


class MacOsLoaderObservationTests(unittest.TestCase):
    def test_application_override_does_not_retarget_inspector_in_same_directory(self):
        with patch("pathlib.Path.exists", return_value=True):
            root = PurePosixPath("/fixture")
            app, tool = root / "app", root / "inspector"
            original = str(root / "original" / "libproof.dylib")
            replacement = str(root / "override" / "libproof.dylib")
            observer = FixtureObserver(
                {
                    str(app): ((original, False),),
                    str(tool): ((original, False),),
                    original: (),
                    replacement: (),
                },
                MacOsLoaderPaths(library=(str(root / "override"),)),
            )
            _, edges = observer._closure(
                {str(app): {"runtime"}, str(tool): {"build", "toolchain"}}
            )
            self.assertIn((str(app), replacement), edges)
            self.assertIn((str(tool), original), edges)
            self.assertNotIn((str(tool), replacement), edges)
            self.assertNotIn((str(app), original), edges)

    def test_shared_transitive_image_retains_both_process_contexts(self):
        with patch("pathlib.Path.exists", return_value=True):
            root = PurePosixPath("/fixture")
            app, tool = root / "app", root / "inspector"
            shared = str(root / "shared.dylib")
            original = str(root / "original" / "libproof.dylib")
            replacement = str(root / "override" / "libproof.dylib")
            observer = FixtureObserver(
                {
                    str(app): ((shared, False),),
                    str(tool): ((shared, False),),
                    shared: ((original, False),),
                    original: (),
                    replacement: (),
                },
                MacOsLoaderPaths(library=(str(root / "override"),)),
            )
            _, edges = observer._closure({str(app): {"runtime"}, str(tool): {"build"}})
            self.assertIn((shared, replacement), edges)
            self.assertIn((shared, original), edges)

    def test_fallback_applies_only_after_original_lookup_fails(self):
        original, fallback = "/original/libproof.dylib", "/fallback/libproof.dylib"
        paths = MacOsLoaderPaths(fallback_library=("/fallback",))
        observer = FixtureObserver({original: (), fallback: ()}, paths)
        args = dict(
            loader_dir="/app", executable_dir="/app", rpaths=(), loader_paths=paths
        )
        self.assertEqual(observer._resolve_load_path(original, **args), original)
        del observer.images[original]
        self.assertEqual(observer._resolve_load_path(original, **args), fallback)
        self.assertEqual(
            observer._resolve_load_path("libproof.dylib", **args), fallback
        )
