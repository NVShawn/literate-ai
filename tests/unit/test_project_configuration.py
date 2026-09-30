from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai import projects as projects_module
from literate_ai.contracts import (
    ContractValidationError,
    ProjectDefinition,
    RepositoryMainState,
    RepositoryPolicy,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectConfigurationStore,
    ProjectError,
    parse_project_configuration,
    serialize_project_configuration,
)

ROOT = Path(__file__).resolve().parents[2]


class _FakeStatResult:
    """Minimal stand-in for ``os.stat_result`` exposing the inode fields.

    Only ``st_ino``/``st_dev`` are consulted by ``_lock_identity_matches``; a
    real ``os.stat_result`` cannot be constructed with a zero inode on a live
    file, so this fake lets the Windows ``st_ino == 0`` branch be exercised from
    a POSIX host.
    """

    def __init__(self, st_dev: int, st_ino: int) -> None:
        self.st_dev = st_dev
        self.st_ino = st_ino


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


class RepositoryPolicyTests(unittest.TestCase):
    def test_defaults_and_omitted_project_policy(self) -> None:
        policy = RepositoryPolicy.from_dict({"schema": RepositoryPolicy.SCHEMA})
        self.assertEqual(policy, RepositoryPolicy())
        self.assertEqual(policy.release_ci_workflow, "CI")
        wire = _definition().to_dict()
        wire.pop("repository_policy")
        self.assertEqual(ProjectDefinition.from_dict(wire).repository_policy, policy)

    def test_policy_validation(self) -> None:
        invalid = (
            {"main_state": RepositoryMainState.PRE_RELEASE},
            {"default_branch": "bad..branch"},
            {"work_queue": "../queue.md"},
            {"writers": ("bad writer",)},
            {"branch_gc_minimum_age_days": 0},
        )
        for fields in invalid:
            with (
                self.subTest(fields=fields),
                self.assertRaises(ContractValidationError),
            ):
                RepositoryPolicy(**fields)

    def test_schema_includes_policy_project_type_and_log_dir(self) -> None:
        definition = replace(
            _definition(), project_type="application", log_dir="var/log"
        )
        schema = json.loads(
            (ROOT / "schemas/v2/projects-skills.schema.json").read_bytes()
        )
        project_properties = schema["$defs"]["project"]["properties"]
        self.assertIn("project_type", project_properties)
        self.assertIn("log_dir", project_properties)
        self.assertEqual(
            project_properties["repository_policy"]["$ref"], RepositoryPolicy.SCHEMA
        )
        self.assertEqual(ProjectDefinition.from_dict(definition.to_dict()), definition)


class ProjectConfigurationStoreTests(unittest.TestCase):
    def test_exact_bytes_differ_despite_semantic_identity(self) -> None:
        first = serialize_project_configuration(_definition())
        second = json.dumps(json.loads(first), separators=(",", ":")).encode()
        self.assertEqual(
            parse_project_configuration(first), parse_project_configuration(second)
        )
        self.assertNotEqual(first, second)

    def test_invalid_and_symlink_reads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / PROJECT_FILENAME
            manifest.write_bytes(b"not json")
            with self.assertRaises(ProjectError):
                ProjectConfigurationStore(root).read()
            manifest.unlink()
            target = root / "target.json"
            target.write_bytes(serialize_project_configuration(_definition()))
            manifest.symlink_to(target)
            with self.assertRaises(ProjectError):
                ProjectConfigurationStore(root).read()

    def test_oversized_manifest_is_rejected_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / PROJECT_FILENAME).write_bytes(b" " * 33)
            with self.assertRaisesRegex(ProjectError, "byte limit"):
                ProjectConfigurationStore(root, maximum_bytes=32).read()

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

    @unittest.skipIf(os.name == "nt", "POSIX lock replacement uses flock")
    def test_lock_rejects_path_replacement_after_acquisition(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectConfigurationStore(root)
            original_flock = fcntl.flock
            replaced = False

            def replace_after_lock(descriptor: int, operation: int) -> None:
                nonlocal replaced
                original_flock(descriptor, operation)
                if operation == fcntl.LOCK_EX and not replaced:
                    replaced = True
                    lock_path = root / f".{PROJECT_FILENAME}.write.lock"
                    displaced = root / "displaced.lock"
                    lock_path.replace(displaced)
                    lock_path.write_bytes(b"replacement")

            with (
                mock.patch("fcntl.flock", replace_after_lock),
                self.assertRaisesRegex(ProjectError, "changed while it was acquired"),
            ):
                store.create(_definition())
            self.assertEqual(
                (root / f".{PROJECT_FILENAME}.write.lock").read_bytes(),
                b"replacement",
            )

    def test_create_typed_update_noop_and_stale_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectConfigurationStore(Path(directory) / "project")
            created = store.create(_definition())
            with self.assertRaisesRegex(ProjectError, "already exists"):
                store.create(_definition())
            inode = store.path.stat().st_ino
            self.assertEqual(
                store.update(created, created.definition).content, created.content
            )
            self.assertEqual(store.path.stat().st_ino, inode)
            updated = store.update(
                created, replace(created.definition, project_id="updated")
            )
            self.assertEqual(updated.definition.project_id, "updated")
            with self.assertRaisesRegex(ProjectError, "changed since it was read"):
                store.update(created, created.definition)
            self.assertFalse((store.root / f".{PROJECT_FILENAME}.write.lock").exists())

    def test_format_only_change_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectConfigurationStore(Path(directory))
            snapshot = store.create(_definition())
            store.path.write_bytes(json.dumps(json.loads(snapshot.content)).encode())
            with self.assertRaisesRegex(ProjectError, "changed since it was read"):
                store.update(snapshot, snapshot.definition)

    @unittest.skipIf(os.name == "nt", "POSIX idle cleanup unlinks the lock inode")
    def test_lock_retries_when_idle_cleanup_unlinks_just_opened_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = ProjectConfigurationStore(root)
            lock_path = root / f".{PROJECT_FILENAME}.write.lock"
            original_open = os.open
            removed = False

            def open_then_remove(path: object, flags: int, *args: object) -> int:
                nonlocal removed
                descriptor = original_open(path, flags, *args)
                if Path(path) == lock_path and not removed:
                    removed = True
                    lock_path.unlink()
                return descriptor

            with mock.patch("literate_ai.projects.os.open", open_then_remove):
                store.create(_definition())

            self.assertTrue(removed)
            self.assertFalse(lock_path.exists())

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

    def test_posix_identity_uses_dev_and_ino_and_never_calls_win32(self) -> None:
        called = False

        def fail_windows(*_args: object, **_kwargs: object) -> bool:
            nonlocal called
            called = True
            return True

        with (
            mock.patch.object(projects_module, "_is_windows", lambda: False),
            mock.patch.object(
                projects_module, "_windows_identity_matches", fail_windows
            ),
        ):
            same = _FakeStatResult(st_dev=7, st_ino=42)
            other = _FakeStatResult(st_dev=7, st_ino=43)
            self.assertTrue(
                ProjectConfigurationStore._lock_identity_matches(
                    Path("x"), same, 5, same
                )
            )
            self.assertFalse(
                ProjectConfigurationStore._lock_identity_matches(
                    Path("x"), same, 5, other
                )
            )
        self.assertFalse(called, "POSIX path must not touch Win32 identity APIs")

    def test_windows_zero_inode_routes_to_win32_identity(self) -> None:
        # Root cause #2: on Windows `os.fstat` of an `os.open` descriptor can
        # report st_ino == 0, so the bare (st_dev, st_ino) compare is unreliable.
        # In that case identity must be resolved via GetFileInformationByHandle.
        observed: dict[str, object] = {}

        def record(path: Path, descriptor: int) -> bool:
            observed["path"] = path
            observed["descriptor"] = descriptor
            return True

        with (
            mock.patch.object(projects_module, "_is_windows", lambda: True),
            mock.patch.object(projects_module, "_windows_identity_matches", record),
        ):
            zero = _FakeStatResult(st_dev=0, st_ino=0)
            self.assertTrue(
                ProjectConfigurationStore._lock_identity_matches(
                    Path("C:\\lock"), zero, 11, zero
                )
            )
        self.assertEqual(observed["descriptor"], 11)
        self.assertEqual(observed["path"], Path("C:\\lock"))

    def test_windows_real_inode_still_uses_dev_and_ino(self) -> None:
        # When Windows does supply a real inode the fast path must remain the
        # exact (st_dev, st_ino) compare -- Win32 identity is only the fallback.
        def fail_windows(*_args: object, **_kwargs: object) -> bool:
            self.fail("must not call Win32 identity when a real inode exists")

        with (
            mock.patch.object(projects_module, "_is_windows", lambda: True),
            mock.patch.object(
                projects_module, "_windows_identity_matches", fail_windows
            ),
        ):
            here = _FakeStatResult(st_dev=3, st_ino=99)
            there = _FakeStatResult(st_dev=3, st_ino=100)
            self.assertTrue(
                ProjectConfigurationStore._lock_identity_matches(
                    Path("C:\\lock"), here, 5, here
                )
            )
            self.assertFalse(
                ProjectConfigurationStore._lock_identity_matches(
                    Path("C:\\lock"), here, 5, there
                )
            )

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


class ProjectManifestLockSerializationTests(unittest.TestCase):
    """The lock must take a real exclusive lock, not a no-op, on this host."""

    def test_lock_serializes_two_concurrent_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProjectConfigurationStore(Path(directory))
            entered = threading.Event()
            release = threading.Event()
            overlap = []

            def hold_lock() -> None:
                root = store._root(create=True)
                with store._lock(root):
                    entered.set()
                    # Hold the lock until the main thread has confirmed the
                    # second acquisition cannot proceed concurrently.
                    release.wait(2.0)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            self.assertTrue(entered.wait(2.0))

            acquired_second = threading.Event()

            def contend() -> None:
                root = store._root(create=True)
                with store._lock(root):
                    acquired_second.set()

            waiter = threading.Thread(target=contend)
            waiter.start()
            # While the first thread still holds the lock the second must block.
            overlap.append(acquired_second.wait(0.3))
            release.set()
            waiter.join(3.0)
            holder.join(3.0)

            self.assertFalse(
                overlap[0],
                "second writer acquired the lock while the first still held it",
            )
            self.assertTrue(
                acquired_second.is_set(),
                "second writer never acquired the lock after release",
            )


if __name__ == "__main__":
    unittest.main()
