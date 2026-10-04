"""Small state-machine checks complementing the real-Git refresh journeys."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_application as refresh
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_application import (
    _LiveRefreshApplication,
    _State,
)


class RefreshApplicationTransitionTests(unittest.TestCase):
    def application(self):
        application = object.__new__(_LiveRefreshApplication)
        application._targets = {}
        application._undos = []
        application._retained_custody = []
        return application

    def test_race_replacement_is_restored_without_clobbering_foreign_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "source"
            path.write_bytes(b"reviewed before")
            application = self.application()
            before = refresh._state(path)
            injected = False

            def replace_after_revalidation(selected, _observed):
                nonlocal injected
                if selected != path or injected:
                    return
                injected = True
                replacement = root / "foreign"
                replacement.write_bytes(b"foreign replacement")
                os.replace(replacement, path)

            with (
                patch.object(
                    refresh, "_before_materialize", replace_after_revalidation
                ),
                self.assertRaises(OrchestrationInventoryError),
            ):
                application.transition(
                    path,
                    before,
                    _State("file", b"prospective", 0o644),
                    "child:source",
                )
            self.assertEqual(path.read_bytes(), b"foreign replacement")
            self.assertEqual(
                [item.name for item in root.iterdir()],
                ["source"],
            )

    @unittest.skipIf(os.name == "nt", "Windows does not rename an open CRT file")
    def test_open_descriptor_change_is_retained_before_owned_inode_discard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "source"
            path.write_bytes(b"reviewed before")
            descriptor = os.open(path, os.O_WRONLY)
            application = self.application()
            before = refresh._state(path)
            injected = False

            def mutate_renamed_inode(_owned):
                nonlocal injected
                if injected:
                    return
                injected = True
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"foreign through fd")
                os.ftruncate(descriptor, len(b"foreign through fd"))
                os.fsync(descriptor)

            application.transition(
                path,
                before,
                _State("file", b"prospective", 0o644),
                "child:source",
            )
            try:
                with (
                    patch.object(refresh, "_before_discard", mutate_renamed_inode),
                    self.assertRaises(OrchestrationInventoryError),
                ):
                    application.discard_replaced_custody()
            finally:
                os.close(descriptor)
            retained = [
                item
                for item in root.iterdir()
                if item.name.startswith(".litai-refresh-")
            ]
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"foreign through fd")
            application.rollback()
            self.assertEqual(path.read_bytes(), b"reviewed before")
            self.assertEqual(retained[0].read_bytes(), b"foreign through fd")


if __name__ == "__main__":
    unittest.main()
