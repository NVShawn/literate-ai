"""Small state-machine checks complementing the real-Git refresh journeys."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_application as refresh
from literate_ai.adapters.repository_file_custody import DirectoryCustody
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

    def test_type_transition_tracks_one_terminal_before_and_prospective_state(self):
        application = object.__new__(_LiveRefreshApplication)
        application._targets = {}
        path = Path("/repository/child/node")
        before = _State("file", b"before", 0o644)
        absent = _State("absent")
        prospective = _State("symlink", b"target")

        application._register(path, before, absent, "child:node")
        application._register(path, absent, prospective, "child:node")

        self.assertEqual(
            application._targets[path], (before, prospective, "child:node")
        )

    @unittest.skipIf(os.name == "nt", "POSIX directory modes and symlinks")
    def test_directory_add_remove_and_type_transitions_restore_exact_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            removed = root / "removed"
            removed.mkdir()
            removed.chmod(0o711)
            application = self.application()
            before = refresh._state(removed)
            application.transition(removed, before, _State("absent"), "child:removed")
            self.assertFalse(removed.exists())
            application.rollback()
            self.assertEqual(stat.S_IMODE(removed.stat().st_mode), 0o711)

            added = root / "added"
            application = self.application()
            application.transition(
                added,
                _State("absent"),
                _State("directory", permissions=0o750),
                "child:added",
            )
            self.assertEqual(stat.S_IMODE(added.stat().st_mode), 0o750)
            application.rollback()
            self.assertFalse(added.exists())

            file_to_directory = root / "file-to-directory"
            file_to_directory.write_bytes(b"before")
            file_to_directory.chmod(0o640)
            application = self.application()
            before = refresh._state(file_to_directory)
            application.transition(
                file_to_directory,
                before,
                _State("absent"),
                "child:file-to-directory",
            )
            application.transition(
                file_to_directory,
                _State("absent"),
                _State("directory", permissions=0o751),
                "child:file-to-directory",
            )
            self.assertTrue(file_to_directory.is_dir())
            self.assertEqual(stat.S_IMODE(file_to_directory.stat().st_mode), 0o751)
            application.rollback()
            self.assertEqual(file_to_directory.read_bytes(), b"before")
            self.assertEqual(stat.S_IMODE(file_to_directory.stat().st_mode), 0o640)

            directory_to_symlink = root / "directory-to-symlink"
            directory_to_symlink.mkdir()
            directory_to_symlink.chmod(0o701)
            application = self.application()
            before = refresh._state(directory_to_symlink)
            application.transition(
                directory_to_symlink,
                before,
                _State("absent"),
                "child:directory-to-symlink",
            )
            application.transition(
                directory_to_symlink,
                _State("absent"),
                _State("symlink", b"target"),
                "child:directory-to-symlink",
            )
            self.assertEqual(os.readlink(directory_to_symlink), "target")
            application.rollback()
            self.assertTrue(directory_to_symlink.is_dir())
            self.assertEqual(stat.S_IMODE(directory_to_symlink.stat().st_mode), 0o701)

            symlink_to_file = root / "symlink-to-file"
            symlink_to_file.symlink_to("before-target")
            application = self.application()
            before = refresh._state(symlink_to_file)
            application.transition(
                symlink_to_file,
                before,
                _State("absent"),
                "child:symlink-to-file",
            )
            application.transition(
                symlink_to_file,
                _State("absent"),
                _State("file", b"after", 0o644),
                "child:symlink-to-file",
            )
            self.assertEqual(symlink_to_file.read_bytes(), b"after")
            application.rollback()
            self.assertTrue(symlink_to_file.is_symlink())
            self.assertEqual(os.readlink(symlink_to_file), "before-target")

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

    def test_nested_gitlink_is_opaque_and_requires_existing_child_custody(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            nested = root / "nested"
            nested.mkdir()
            (nested / ".git").write_text("gitdir: elsewhere\n")
            (nested / "owned-source").write_bytes(b"independent child")
            root_node = refresh._signature(root.lstat())[:3]
            nested_node = refresh._signature(nested.lstat())[:3]
            directory = DirectoryCustody(
                ".",
                refresh._signature(root.lstat()),
                ((b"nested", refresh._signature(nested.lstat())),),
            )
            tree = SimpleNamespace(
                entries=(SimpleNamespace(path="nested", mode="160000", content=None),)
            )
            plan = SimpleNamespace(
                root=root,
                root_node=root_node,
                previous=tree,
                prospective=tree,
                policy=SimpleNamespace(maximum_entries=16),
                changes=(),
                directories=(directory,),
            )
            observed = SimpleNamespace(
                root=nested,
                root_node=nested_node,
                hydrated_lfs=(),
            )
            direct = SimpleNamespace(root=root, hydrated_lfs=())
            application = SimpleNamespace(
                owner=SimpleNamespace(
                    _prepared=SimpleNamespace(
                        refresh=SimpleNamespace(children=(direct, observed))
                    )
                ),
                reservations=SimpleNamespace(verify_all=lambda: None),
                _retained_custody=[],
            )
            with patch.object(
                refresh, "require_nested_refresh_child_unchanged"
            ) as verify:
                refresh._require_worktree_state(application, plan, prospective=True)
            verify.assert_called_once_with(
                observed, reservations=application.reservations
            )

            application.owner._prepared.refresh.children = (direct,)
            with self.assertRaises(OrchestrationInventoryError):
                refresh._require_worktree_state(application, plan, prospective=True)

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
