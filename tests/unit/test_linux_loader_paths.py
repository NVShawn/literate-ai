"""Explicit Linux loader environment is bounded, ordered and immutable."""

import unittest
from unittest import mock

from literate_ai.adapters.dependencies.observation import PortableHostDependencyObserver
from literate_ai.adapters.linux_loader_paths import LinuxLoaderPaths


class LinuxLoaderPathsTests(unittest.TestCase):
    def test_portable_observer_forwards_explicit_linux_selection(self):
        paths = LinuxLoaderPaths(("/library",))
        with (
            mock.patch(
                "literate_ai.adapters.dependencies.observation.sys.platform", "linux"
            ),
            mock.patch(
                "literate_ai.adapters.dependencies.observation.LinuxElfDependencyObserver"
            ) as native,
        ):
            observer = PortableHostDependencyObserver(linux_loader_paths=paths)
            observer.observe({}, root_ref="fixture")
        self.assertIs(native.call_args.kwargs["loader_paths"], paths)

    def test_separators_preserve_order_and_equivalent_identity(self):
        paths = LinuxLoaderPaths.from_environment({"LD_LIBRARY_PATH": "/a;/b:/a"})
        self.assertEqual(paths.library, ("/a", "/b", "/a"))
        self.assertEqual(paths.to_environment(), {"LD_LIBRARY_PATH": "/a:/b:/a"})
        self.assertEqual(paths.identity, LinuxLoaderPaths(("/a", "/b", "/a")).identity)

    def test_empty_variable_has_no_search_directories(self):
        self.assertEqual(
            LinuxLoaderPaths.from_environment({"LD_LIBRARY_PATH": ""}),
            LinuxLoaderPaths(),
        )

    def test_caller_mutation_cannot_change_selection(self):
        environment = {"LD_LIBRARY_PATH": "/a", "PATH": "/usr/bin"}
        paths = LinuxLoaderPaths.from_environment(environment)
        environment["LD_LIBRARY_PATH"] = "/b"
        paths.to_environment()["LD_LIBRARY_PATH"] = "/c"
        self.assertEqual(paths.library, ("/a",))

    def test_unmodeled_controls_refuse_even_when_empty(self):
        for key in (
            "LD_PRELOAD",
            "LD_AUDIT",
            "LD_DEBUG",
            "LD_HWCAP_MASK",
            "GLIBC_TUNABLES",
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "control-unsupported"),
            ):
                LinuxLoaderPaths.from_environment({key: ""})

    def test_unsafe_or_context_dependent_paths_refuse(self):
        for value in (
            "relative",
            "/a:",
            ":/a",
            "/a;;/b",
            "/a/../b",
            "/a/",
            "//a",
            "/a/./b",
            "$ORIGIN/lib",
            "/$LIB",
            "/a\x00b",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LinuxLoaderPaths.from_environment({"LD_LIBRARY_PATH": value})

    def test_bounds_and_direct_constructor_validation(self):
        for value in (("/a",) * 129, ["/a"], (None,), ("/" + "a" * 4096,)):
            with self.subTest(value=type(value)), self.assertRaises(ValueError):
                LinuxLoaderPaths(value)
        self.assertEqual(len(LinuxLoaderPaths(("/a",) * 128).library), 128)

    def test_environment_types_and_size_refuse(self):
        for environment in (
            [],
            {"LD_LIBRARY_PATH": None},
            {1: "value"},
            {str(i): "x" for i in range(2049)},
        ):
            with self.subTest(type=type(environment)), self.assertRaises(ValueError):
                LinuxLoaderPaths.from_environment(environment)
