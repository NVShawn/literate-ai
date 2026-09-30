"""Explicit native seeds retain their actual paths without scanning build debris."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.dependencies import observation


class DependencyArtifactSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ns-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.binary = self.root / "target" / "test-binary"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"\xcf\xfa\xed\xfe")

    def test_portable_selection_is_copied_and_forwarded(self):
        for platform, adapter in (
            ("darwin", "MacOsMachODependencyObserver"),
            ("linux", "LinuxElfDependencyObserver"),
            ("win32", "WindowsPeDependencyObserver"),
        ):
            selection = [self.binary]
            observer = observation.PortableHostDependencyObserver(
                artifact_files=selection
            )
            selection.clear()
            with (
                self.subTest(platform=platform),
                patch.object(observation.sys, "platform", platform),
                patch.object(observation, adapter) as selected,
            ):
                observer.observe({}, root_ref="fixture")
                self.assertEqual(
                    selected.call_args.kwargs["artifact_files"], (self.binary,)
                )

    def test_only_selected_native_path_is_inspected_in_place(self):
        (self.binary.parent / "object.o").write_bytes(b"\xcf\xfa\xed\xfeother")
        with patch.object(
            observation,
            "_regular_tree_files",
            side_effect=AssertionError("must not scan unrelated files"),
        ):
            self.assertEqual(
                observation._native_artifact_files(
                    self.root,
                    (self.binary,),
                    observation._is_macho,
                ),
                (self.binary,),
            )

    def test_default_selection_still_scans_and_filters_the_tree(self):
        (self.root / "data").write_bytes(b"data")
        self.assertEqual(
            observation._native_artifact_files(
                self.root,
                None,
                observation._is_macho,
            ),
            (self.binary,),
        )

    def test_empty_duplicate_relative_traversing_and_unbounded_selections_refuse(self):
        for selection in (
            (),
            (self.binary, self.binary),
            (Path("relative"),),
            (self.root / ".." / "file",),
            (self.binary,) * 4097,
        ):
            for adapter in (
                observation.PortableHostDependencyObserver,
                observation.MacOsMachODependencyObserver,
                observation.LinuxElfDependencyObserver,
                observation.WindowsPeDependencyObserver,
            ):
                with (
                    self.subTest(adapter=adapter.__name__),
                    self.assertRaises(ValueError),
                ):
                    adapter(artifact_files=selection)

    def test_missing_outside_non_native_and_directory_selections_refuse(self):
        data = self.root / "data"
        data.write_bytes(b"data")
        for path in (
            self.root / "missing",
            self.root.parent / "outside",
            data,
            self.binary.parent,
        ):
            with (
                self.subTest(path=path),
                self.assertRaises(observation.DependencyObservationError),
            ):
                observation._native_artifact_files(
                    self.root, (path,), observation._is_macho
                )

    def test_file_and_parent_links_refuse(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.binary)
        except OSError:
            self.skipTest("host does not permit symbolic links")
        parent = self.root / "linked-parent"
        parent.symlink_to(self.binary.parent, target_is_directory=True)
        for path in (alias, parent / self.binary.name):
            with (
                self.subTest(path=path),
                self.assertRaises(observation.DependencyObservationError),
            ):
                observation._native_artifact_files(
                    self.root, (path,), observation._is_macho
                )
