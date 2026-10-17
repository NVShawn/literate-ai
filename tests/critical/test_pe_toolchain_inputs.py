"""Exact PE DLL inputs for native dependency inspection, without execution."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.dependencies.observation import _resolve_pe_toolchain_input
from literate_ai.adapters.dependencies.types import DependencyObservationError


def _pe_header():
    header = bytearray(64)
    header[:2] = b"MZ"
    header[60:64] = (64).to_bytes(4, "little")
    return bytes(header) + b"PE\0\0"


class PeToolchainInputTests(unittest.TestCase):
    def test_explicit_dll_is_inspected_without_command_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "beam.smp.dll"
            path.write_bytes(_pe_header())
            with mock.patch("shutil.which") as which:
                self.assertEqual(_resolve_pe_toolchain_input((str(path),)), path)
            which.assert_not_called()

    def test_dll_input_rejects_relative_paths_arguments_and_wrong_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "beam.smp.dll"
            path.write_bytes(b"not a PE file")
            for command in (("beam.smp.dll",), (str(path),), (str(path), "--version")):
                with self.subTest(command=command):
                    with self.assertRaises(DependencyObservationError):
                        _resolve_pe_toolchain_input(command)

    def test_dll_input_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "payload.dll"
            target.write_bytes(_pe_header())
            link = root / "beam.smp.dll"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("host does not permit symlink creation")
            with self.assertRaises(DependencyObservationError):
                _resolve_pe_toolchain_input((str(link),))

    def test_executable_command_keeps_normal_discovery(self):
        path = Path(sys.executable).resolve()
        self.assertEqual(_resolve_pe_toolchain_input((str(path),)), path)
