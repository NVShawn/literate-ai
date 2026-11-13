"""Windows cleanup control flow with explicit native-handle fixtures.

Real files, hardlinks, chmod and recursive cleanup run on this host. An injected
readonly unlink refusal models the Windows error boundary. These controls do not
qualify actual Windows APIs, sharing, disposition or readonly attributes.
"""

import ctypes
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import qualification_workspace_namespace as namespace


class WindowsRetirementControlFlowTests(unittest.TestCase):
    def exercise(self, *, depth, linked=True, replace=False, reparse=False):
        with tempfile.TemporaryDirectory(prefix="authored-windows-retirement-") as temp:
            root = Path(temp)
            container = root / "container"
            run = container / "run"
            run.mkdir(parents=True)
            external = root / "external"
            external.write_text("external bytes")
            destination = run
            for _ in range(depth):
                destination = destination / "nested"
                destination.mkdir()
            leaf = destination / "readonly"
            if reparse:
                try:
                    leaf.symlink_to(external)
                except (OSError, NotImplementedError):
                    self.skipTest("host cannot create a directory-entry link")
            elif linked:
                os.link(external, leaf)
            else:
                leaf.write_text("owned bytes")
            external.chmod(0o400)
            if not reparse:
                leaf.chmod(0o400)
            before_mode = stat.S_IMODE(external.stat().st_mode)
            real_unlink, denied, closed, dispositions = os.unlink, False, [], []

            def refuse_readonly_once(path, *args, **kwargs):
                nonlocal denied
                if Path(path).name == "readonly" and not denied:
                    denied = True
                    if replace:
                        Path(path).rename(root / "original")
                        Path(path).write_text("foreign bytes")
                        Path(path).chmod(0o400)
                    raise PermissionError("injected readonly-file refusal")
                return real_unlink(path, *args, **kwargs)

            class KernelFixture:
                _invalid_handle = -1

                def open_reparse_aware(self, *args, **kwargs):
                    return 1

                def _create_file(self, *args):
                    return 2

                def file_identity(self, handle):
                    return 1, 0, handle

                def close(self, handle):
                    closed.append(handle)

            class DispositionFixture:
                def __call__(self, *args):
                    dispositions.append(args[1])
                    return True

            error = None
            with (
                patch("literate_ai._cache_lock._Kernel32Windows", KernelFixture),
                patch.object(
                    ctypes,
                    "WinDLL",
                    return_value=SimpleNamespace(
                        SetFileInformationByHandle=DispositionFixture()
                    ),
                    create=True,
                ),
                patch.object(os, "unlink", refuse_readonly_once),
            ):
                try:
                    namespace._retire_windows_run(
                        run, (1, 0, 2), container, (1, 0, 1), lambda: None
                    )
                except (PermissionError, ValueError) as exc:
                    error = exc
            self.assertEqual(closed, [2, 1])
            self.assertEqual(external.read_text(), "external bytes")
            self.assertEqual(stat.S_IMODE(external.stat().st_mode), before_mode)
            if linked and not reparse:
                self.assertIsInstance(error, PermissionError)
                self.assertTrue(denied)
                self.assertEqual(external.stat().st_nlink, 2)
                self.assertEqual(leaf.read_text(), "external bytes")
                self.assertFalse(dispositions)
            elif reparse:
                self.assertIsInstance(error, ValueError)
                self.assertTrue(leaf.is_symlink())
                self.assertFalse(denied)
                self.assertFalse(dispositions)
            elif replace:
                self.assertIsInstance(error, PermissionError)
                self.assertEqual(leaf.read_text(), "foreign bytes")
                self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o400)
                self.assertEqual((root / "original").read_text(), "owned bytes")
                self.assertFalse(dispositions)
            else:
                self.assertIsNone(error)
                self.assertTrue(denied)
                self.assertFalse(leaf.exists())
                self.assertEqual(dispositions, [4])

    def test_direct_readonly_hardlink_refuses_without_external_permission_change(self):
        self.exercise(depth=0)

    def test_nested_readonly_hardlink_refuses_without_external_permission_change(self):
        self.exercise(depth=1)

    def test_deeper_readonly_hardlink_uses_same_guard(self):
        self.exercise(depth=2)

    def test_owned_direct_readonly_file_can_retry(self):
        self.exercise(depth=0, linked=False)

    def test_owned_nested_readonly_file_can_retry(self):
        self.exercise(depth=2, linked=False)

    def test_nested_reparse_refuses_before_permission_change(self):
        self.exercise(depth=1, linked=False, reparse=True)

    def test_replaced_leaf_is_not_made_writable(self):
        self.exercise(depth=1, linked=False, replace=True)
