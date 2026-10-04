"""Explicit runtime roots reach native observation and preserve ambiguity refusal."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.dependencies import observation
from tests.support import fixtures_test_dependency_lifecycle as fixtures


class DependencyLibraryRootTests(unittest.TestCase):
    def test_portable_observer_routes_explicit_roots_on_every_supported_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for platform, adapter in (
                ("darwin", "MacOsMachODependencyObserver"),
                ("linux", "LinuxElfDependencyObserver"),
                ("win32", "WindowsPeDependencyObserver"),
            ):
                with (
                    self.subTest(platform=platform),
                    patch.object(observation.sys, "platform", platform),
                    patch.object(observation, adapter) as selected,
                ):
                    expected = selected.return_value.observe.return_value
                    result = observation.PortableHostDependencyObserver(
                        library_roots=(root, root)
                    ).observe({}, root_ref="fixture")
                    self.assertIs(result, expected)
                    self.assertEqual(
                        selected.call_args.kwargs["library_roots"], (root,)
                    )

    def test_macho_named_runtime_can_resolve_without_embedded_rpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = str(root / "libfixture.dylib")
            observer = fixtures._FixtureMacObserver({target})
            observer.library_roots = (root,)
            for name in ("@rpath/libfixture.dylib", "libfixture.dylib"):
                self.assertEqual(
                    observer._resolve_load_path(
                        name,
                        loader_dir="/fixture",
                        executable_dir="/fixture",
                        rpaths=(),
                    ),
                    target,
                )

    def test_multiple_matching_runtime_roots_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            roots = (root / "first", root / "second")
            observer = fixtures._FixtureMacObserver(
                {str(p / "libfixture.dylib") for p in roots}
            )
            observer.library_roots = roots
            with self.assertRaises(observation.DependencyObservationError):
                observer._resolve_load_path(
                    "@rpath/libfixture.dylib",
                    loader_dir="/fixture",
                    executable_dir="/fixture",
                    rpaths=(),
                )

    def test_relative_traversing_and_unbounded_roots_refuse_before_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for roots in ((Path("relative"),), (root / "..",), (root,) * 129):
                for adapter in (
                    observation.PortableHostDependencyObserver,
                    observation.MacOsMachODependencyObserver,
                    observation.LinuxElfDependencyObserver,
                    observation.WindowsPeDependencyObserver,
                ):
                    with (
                        self.subTest(roots=roots[:1], adapter=adapter.__name__),
                        self.assertRaises(ValueError),
                    ):
                        adapter(library_roots=roots)
