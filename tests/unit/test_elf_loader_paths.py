"""Dynamic tags retain loader precedence and refuse implicit cwd entries."""

import unittest

from literate_ai.adapters.dependencies.observation import (
    DependencyObservationError,
    parse_readelf_dynamic,
)


class ElfLoaderPathTests(unittest.TestCase):
    def test_runpath_suppresses_rpath_regardless_of_tag_order(self):
        tags = (
            "0x (RPATH) Library rpath: [/old]\n",
            "0x (RUNPATH) Library runpath: [/new:/second]\n",
        )
        for text in ("".join(tags), "".join(reversed(tags))):
            with self.subTest(text=text):
                self.assertEqual(parse_readelf_dynamic(text), ((), ("/new", "/second")))

    def test_empty_runpath_still_suppresses_rpath(self):
        self.assertEqual(
            parse_readelf_dynamic(
                "0x (RPATH) Library rpath: [/old]\n0x (RUNPATH) Library runpath: []\n"
            ),
            ((), ()),
        )

    def test_empty_path_component_cannot_be_silently_discarded(self):
        for tag in ("RPATH", "RUNPATH"):
            for paths in (":/lib", "/lib:", "/lib::/other"):
                with self.subTest(tag=tag, paths=paths):
                    with self.assertRaises(DependencyObservationError) as caught:
                        parse_readelf_dynamic(f"0x ({tag}) paths: [{paths}]\n")
                    self.assertEqual(
                        caught.exception.code, "dependencies.elf-search-path-unsafe"
                    )

    def test_rpath_remains_available_without_runpath(self):
        self.assertEqual(
            parse_readelf_dynamic(
                "0x (NEEDED) Shared library: [provider.so]\n"
                "0x (RPATH) Library rpath: [$ORIGIN/lib:/other]\n"
            ),
            (("provider.so",), ("$ORIGIN/lib", "/other")),
        )
