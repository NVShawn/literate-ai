from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.contracts import (
    ProjectDefinition,
    RepositoryPolicy,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectConfigurationStore,
    ProjectError,
    serialize_project_configuration,
)

ROOT = Path(__file__).resolve().parents[2]


def _concurrent_update(
    root: str, snapshot: object, project_id: str, queue: object
) -> None:
    assert hasattr(snapshot, "definition")
    try:
        ProjectConfigurationStore(Path(root)).update(
            snapshot, replace(snapshot.definition, project_id=project_id)
        )
    except ProjectError as exc:
        queue.put(exc.code)
    else:
        queue.put("updated")


def _definition() -> ProjectDefinition:
    value = json.loads((ROOT / PROJECT_FILENAME).read_bytes())
    value["project_id"] = "configuration-test"
    value["repository_policy"] = RepositoryPolicy().to_dict()
    return ProjectDefinition.from_dict(value)


class ProjectConfigurationStoreTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX replacement uses an open inode")
    def test_read_rejects_path_replacement_after_descriptor_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectConfigurationStore(root)
            store.create(_definition())
            original_open = os.open
            replaced = False

            def replace_after_open(path: object, flags: int, *args: object) -> int:
                nonlocal replaced
                descriptor = original_open(path, flags, *args)
                if Path(path).name == PROJECT_FILENAME and not replaced:
                    replaced = True
                    replacement = root / "replacement.json"
                    replacement.write_bytes(
                        serialize_project_configuration(_definition())
                    )
                    replacement.replace(store.path)
                return descriptor

            with (
                mock.patch("literate_ai.projects.os.open", replace_after_open),
                self.assertRaisesRegex(ProjectError, "unsafe"),
            ):
                store.read()

    @unittest.skipIf(os.name == "nt", "spawned descriptor timing differs on Windows")
    def test_concurrent_updates_allow_one_exact_snapshot_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectConfigurationStore(root)
            snapshot = store.create(_definition())
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            processes = tuple(
                context.Process(
                    target=_concurrent_update,
                    args=(str(root), snapshot, f"writer-{index}", queue),
                )
                for index in range(2)
            )
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(
                sorted(queue.get(timeout=2) for _process in processes),
                ["project.manifest_stale", "updated"],
            )
            self.assertFalse((root / f".{PROJECT_FILENAME}.write.lock").exists())


class ProjectManifestLockWindowsTests(unittest.TestCase):
    """Cross-platform coverage of the Windows-specific lock hardening."""

    def test_symlinked_lock_file_is_rejected(self) -> None:
        # The lock must never accept a symlink standing in for its lock file,
        # on any platform. O_NOFOLLOW makes os.open fail; if it were bypassed
        # the S_ISLNK guard would still reject it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectConfigurationStore(root)
            lock_path = root / f".{PROJECT_FILENAME}.write.lock"
            target = root / "attacker-owned.lock"
            target.write_bytes(b"\0")
            lock_path.symlink_to(target)
            with self.assertRaisesRegex(ProjectError, "manifest_lock|unsafe"):
                store.create(_definition())


if __name__ == "__main__":
    unittest.main()
