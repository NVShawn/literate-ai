"""Loader override ordering, framework separation and bounded explicit inputs."""

import unittest

from literate_ai.adapters.macos_loader_paths import MacOsLoaderPaths


class MacOsLoaderPathTests(unittest.TestCase):
    def test_library_override_and_fallback_keep_declared_order(self):
        paths = MacOsLoaderPaths.from_environment(
            {
                "DYLD_LIBRARY_PATH": "/first:/second",
                "DYLD_FALLBACK_LIBRARY_PATH": "/fallback",
            }
        )
        for name in (
            "/original/libproof.dylib",
            "@rpath/libproof.dylib",
            "libproof.dylib",
        ):
            self.assertEqual(
                paths.candidates(name),
                (
                    ("/first/libproof.dylib", "/second/libproof.dylib"),
                    ("/fallback/libproof.dylib",),
                ),
            )

    def test_framework_does_not_use_library_leaf_override(self):
        paths = MacOsLoaderPaths.from_environment(
            {
                "DYLD_LIBRARY_PATH": "/wrong",
                "DYLD_FRAMEWORK_PATH": "/frameworks",
                "DYLD_FALLBACK_FRAMEWORK_PATH": "/fallback",
            }
        )
        self.assertEqual(
            paths.candidates("/original/Proof.framework/Versions/A/Proof"),
            (
                ("/frameworks/Proof.framework/Versions/A/Proof",),
                ("/fallback/Proof.framework/Versions/A/Proof",),
            ),
        )
        self.assertEqual(
            paths.candidates("/original/Proof.framework/Proof")[0],
            ("/frameworks/Proof.framework/Proof",),
        )

    def test_nested_framework_uses_inner_framework_suffix(self):
        paths = MacOsLoaderPaths(framework=("/override",))
        self.assertEqual(
            paths.candidates(
                "/Outer.framework/Frameworks/Inner.framework/Versions/A/Inner"
            )[0],
            ("/override/Inner.framework/Versions/A/Inner",),
        )

    def test_environment_mutation_does_not_retarget_projection(self):
        env = {"DYLD_LIBRARY_PATH": "/original"}
        paths = MacOsLoaderPaths.from_environment(env)
        identity = paths.identity
        env["DYLD_LIBRARY_PATH"] = "/changed"
        self.assertEqual(paths.identity, identity)
        self.assertEqual(
            paths.candidates("libproof.dylib")[0], ("/original/libproof.dylib",)
        )

    def test_unsupported_loader_controls_refuse(self):
        for key in (
            "DYLD_INSERT_LIBRARIES",
            "DYLD_IMAGE_SUFFIX",
            "DYLD_VERSIONED_LIBRARY_PATH",
            "DYLD_ROOT_PATH",
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "control-unsupported"),
            ):
                MacOsLoaderPaths.from_environment({key: "/selected"})

    def test_paths_refuse_implicit_current_directory_and_traversal(self):
        for value in (
            "",
            ":/library",
            "/library:",
            "relative",
            "/a/../b",
            "/a/./b",
            "//library",
            "/library/",
            "/a\x00b",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MacOsLoaderPaths.from_environment({"DYLD_LIBRARY_PATH": value})

    def test_bound_applies_across_all_path_controls(self):
        with self.assertRaisesRegex(ValueError, "path-limit"):
            MacOsLoaderPaths.from_environment(
                {
                    "DYLD_LIBRARY_PATH": ":".join(["/a"] * 64),
                    "DYLD_FRAMEWORK_PATH": ":".join(["/b"] * 65),
                }
            )

    def test_invalid_imports_refuse(self):
        for path in ("", "/a/../lib.dylib", "lib.dylib/", "lib\x00.dylib"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                MacOsLoaderPaths().candidates(path)
