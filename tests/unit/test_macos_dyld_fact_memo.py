"""dyld_info facts are reused only for unchanged inspector and image identity."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.dependencies import observation
from literate_ai.adapters.dependencies.observation import (
    DependencyObservationError,
    MacOsMachODependencyObserver,
)

SESSION = "37494448-7120-4E49-817A-145630276E40"
SUMMARY = """image [arm64]:
-uuid:
    AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE
-linked_dylibs:
    attributes     load path
                   /usr/lib/libSystem.B.dylib
"""


def _observer(digest="sha256:inspector"):
    observer = MacOsMachODependencyObserver()
    observer._dyld_info = Path("/fixture/dyld_info")
    observer._dyld_digest = digest
    observer._validated_paths = {}
    return observer


@unittest.skipIf(os.name == "nt", "Mach-O image bindings use POSIX paths")
class DyldFactMemoTests(unittest.TestCase):
    def setUp(self):
        observation._DYLD_FACTS.clear()
        self.calls = []

        def run_tool(tool, arguments):
            self.calls.append(tuple(arguments))
            if arguments[0] == "-validate_only" and arguments[-1].endswith("missing"):
                raise DependencyObservationError("fixture.rejected", "absent")
            return SUMMARY

        patches = (
            patch.object(
                MacOsMachODependencyObserver, "_run_tool", staticmethod(run_tool)
            ),
            patch.object(observation, "_macos_boot_session", return_value=SESSION),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(observation._DYLD_FACTS.clear)

    def test_shared_cache_image_is_inspected_once_per_boot_session(self):
        path = "/System/Library/Frameworks/Fixture.framework/Fixture"
        first = _observer()._inspect(path)
        second = _observer()._inspect(path)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_changed_on_disk_image_is_reinspected(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "libfixture.dylib"
            image.write_bytes(b"original")
            _observer()._inspect(str(image))
            _observer()._inspect(str(image))
            self.assertEqual(len(self.calls), 1)
            image.write_bytes(b"replaced")
            changed = _observer()._inspect(str(image))
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(
                changed.materialized_file,
                observation._macho_materialized_file(image),
            )

    def test_changed_inspector_or_unknown_boot_session_is_not_reused(self):
        path = "/usr/lib/libfixture.dylib"
        _observer("sha256:inspector")._inspect(path)
        _observer("sha256:replaced")._inspect(path)
        self.assertEqual(len(self.calls), 2)
        observation._DYLD_FACTS.clear()
        with patch.object(observation, "_macos_boot_session", return_value=None):
            _observer()._inspect(path)
            _observer()._inspect(path)
        self.assertEqual(len(self.calls), 4)

    def test_fixture_inspectors_bypass_the_memo(self):
        class Fixture(MacOsMachODependencyObserver):
            def _run_dyld(self, arguments):
                return SUMMARY

        observer = Fixture()
        observer._dyld_digest = "sha256:inspector"
        self.assertIsNone(observer._dyld_fact_key("inspect", "/usr/lib/x", None))

    def test_load_candidate_validation_follows_image_identity(self):
        shared = "/usr/lib/libshared.dylib"
        self.assertTrue(_observer()._valid_load_candidate(shared))
        self.assertTrue(_observer()._valid_load_candidate(shared))
        self.assertFalse(_observer()._valid_load_candidate("/usr/lib/missing"))
        self.assertFalse(_observer()._valid_load_candidate("/usr/lib/missing"))
        self.assertEqual(len(self.calls), 2)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "libfixture.dylib"
            image.write_bytes(b"original")
            _observer()._valid_load_candidate(str(image))
            _observer()._valid_load_candidate(str(image))
            image.write_bytes(b"replaced")
            _observer()._valid_load_candidate(str(image))
        self.assertEqual(len(self.calls), 4)


if __name__ == "__main__":
    unittest.main()
