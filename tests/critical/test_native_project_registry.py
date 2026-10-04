"""Adopted child authority must remain local and survive concurrent publication."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest import mock

from literate_ai.adapters import conversion_authority as registry
from literate_ai.projects import ProjectConfigurationStore, load_project
from tests.support.fixtures_test_project_agent_development_workflow import _definition


class NativeProjectRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "adopted"
        ProjectConfigurationStore(self.root).create(_definition(project_id="adopted"))
        self.project = load_project(self.root)

    def child(self, name: str) -> Path:
        child = self.root / ".literate/native-projects" / name
        ProjectConfigurationStore(child).create(_definition(project_id=name))
        return child

    def test_concurrent_successful_registrations_retain_both_children(self):
        first, second = self.child("first"), self.child("second")
        replacing = Event()
        release = Event()
        second_started = Event()
        replace = registry.os.replace

        def hold_first(source, target):
            if not replacing.is_set():
                replacing.set()
                if not release.wait(5):
                    raise AssertionError("first registration was not released")
            return replace(source, target)

        def register_second():
            second_started.set()
            registry.register_native_project(self.project, second)

        with mock.patch.object(registry.os, "replace", side_effect=hold_first):
            with ThreadPoolExecutor(max_workers=2) as executor:
                pending_first = executor.submit(
                    registry.register_native_project, self.project, first
                )
                try:
                    self.assertTrue(replacing.wait(5))
                    pending_second = executor.submit(register_second)
                    self.assertTrue(second_started.wait(5))
                    # The first publication is paused; a second success here would
                    # publish from the same old inventory and lose an entry.
                    with self.assertRaises(TimeoutError):
                        pending_second.result(timeout=0.1)
                finally:
                    release.set()
                pending_first.result(timeout=5)
                pending_second.result(timeout=5)
        self.assertEqual(registry._native_project_roots(self.project), (first, second))

    def test_failed_replace_preserves_existing_registry_and_cleans_temporary(self):
        first, second = self.child("first"), self.child("second")
        registry.register_native_project(self.project, first)
        path = self.root / registry.NATIVE_PROJECTS_FILE
        before = path.read_bytes()
        with mock.patch.object(registry.os, "replace", side_effect=OSError("fault")):
            with self.assertRaises(OSError):
                registry.register_native_project(self.project, second)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(".native-projects.*")), [])
