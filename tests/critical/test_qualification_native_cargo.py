"""Inert archived coordinates, distinct from live filesystem/build custody."""

from __future__ import annotations

import unittest
from pathlib import PurePosixPath, PureWindowsPath
from unittest.mock import patch

from literate_ai.adapters.qualification_cargo import _historical_projection_root


class NativeCargoArchiveCoordinateTests(unittest.TestCase):
    def metadata(self, path):
        return {
            "packages": [{"id": "historical-root", "manifest_path": path}],
            "resolve": {"root": "historical-root"},
        }

    @staticmethod
    def mismatch():
        raise ValueError("archived coordinate refused")

    def test_portable_absolute_coordinates_never_access_historical_filesystem(self):
        cases = [
            (
                "/retired/objects/projection/source/Cargo.toml",
                PurePosixPath("/retired/objects/projection"),
            ),
            (
                r"C:\retired\objects\projection\source\Cargo.toml",
                PureWindowsPath(r"C:\retired\objects\projection"),
            ),
            (
                r"\\server\retired\projection\source\Cargo.toml",
                PureWindowsPath(r"\\server\retired\projection"),
            ),
        ]
        with (
            patch("pathlib.Path.stat") as stat,
            patch("pathlib.Path.resolve") as resolve,
            patch("pathlib.Path.open") as opened,
        ):
            for path, expected in cases:
                with self.subTest(path=path):
                    self.assertEqual(
                        _historical_projection_root(
                            self.metadata(path), "source/Cargo.toml", self.mismatch
                        ),
                        expected,
                    )
            stat.assert_not_called()
            resolve.assert_not_called()
            opened.assert_not_called()

    def test_noncanonical_relative_mixed_or_wrong_manifest_coordinates_refuse(self):
        bad = [
            "retired/source/Cargo.toml",
            "//retired/source/Cargo.toml",
            "/retired\0/source/Cargo.toml",
            "/retired/../foreign/source/Cargo.toml",
            "/retired/./source/Cargo.toml",
            "/retired//source/Cargo.toml",
            "/retired/source/Other.toml",
            "/retired/other/Cargo.toml",
            "/retired\\foreign/source/Cargo.toml",
            r"C:retired\source\Cargo.toml",
            r"C:\retired\..\foreign\source\Cargo.toml",
            "C:/retired/source/Cargo.toml",
            "C:\\retired/source/Cargo.toml",
        ]
        for path in bad:
            with self.subTest(path=path), self.assertRaises(ValueError):
                _historical_projection_root(
                    self.metadata(path), "source/Cargo.toml", self.mismatch
                )

    def test_root_must_be_one_exact_recorded_resolve_member(self):
        missing = self.metadata("/retired/source/Cargo.toml")
        missing["resolve"]["root"] = "foreign"
        duplicated = self.metadata("/retired/source/Cargo.toml")
        duplicated["packages"] *= 2
        for document in (missing, duplicated):
            with self.subTest(metadata=document), self.assertRaises(ValueError):
                _historical_projection_root(
                    document, "source/Cargo.toml", self.mismatch
                )
