from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_python_install``."""

import io

import json

import os

import sys


import unittest

from contextlib import contextmanager

from pathlib import Path

from unittest.mock import patch

from literate_ai.adapters.builders.python import discover_python_toolchain

from literate_ai.adapters.dependencies.python_install import (
    install_python_wheels,
)

from literate_ai.adapters.dependencies.python_lock import parse_python_wheel_lock

from literate_ai.adapters.dependencies.python_target import observe_python_wheel_target

from literate_ai.adapters.dependencies.types import DependencyObservationError

from tests.support import fixtures_test_python_installed as fixtures

class PythonInstallerIntegrationTests(unittest.TestCase):
    """Requires the exact external installer wheel, not network during tests."""

    @classmethod
    def setUpClass(cls):
        value = os.environ.get("LITAI_TEST_PIP_WHEEL")
        if not value:
            raise unittest.SkipTest("Set LITAI_TEST_PIP_WHEEL to the pinned pip wheel")
        cls.installer_path = Path(value).resolve(strict=True)
        cls.toolchain = discover_python_toolchain(pinned_command=sys.executable)
        cls.target = observe_python_wheel_target(cls.toolchain)

    def setUp(self):
        self.fixture = fixtures.PythonInstalledTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.value.update(
            environment=dict(self.target.environment), tags=list(self.target.tags)
        )

    @contextmanager
    def install(self, installer_source=None):
        streams = {
            name: io.BytesIO(content) for name, content in self.fixture.sources.items()
        }
        with self.installer_path.open("rb") as installer:
            with install_python_wheels(
                self.toolchain,
                parse_python_wheel_lock(json.dumps(self.fixture.value)),
                streams,
                installer_source=installer_source or installer,
                temporary_root=self.fixture.root,
            ) as result:
                yield result

    def test_actual_offline_install_and_cleanup(self):
        with self.install() as result:
            path = result.directory
            self.assertEqual(len(result.observation.graph.components), 2)
            self.assertEqual(result.target.identity, self.target.identity)
            self.assertTrue(result.installer_identity.uri.startswith("sha256:"))
            result.revalidate()
        self.assertFalse(path.exists())

    def test_identical_shared_package_files_install_and_revalidate(self):
        for name in self.fixture.sources:
            self.fixture.replace_wheel_files(
                name,
                {"shared/__init__.py": b"raise AssertionError('must not import')\n"},
            )
        with self.install() as result:
            path = result.directory
            result.revalidate()
            self.assertEqual(len(result.observation.graph.components), 2)
            self.assertEqual(
                sum(
                    p.path == "site/shared/__init__.py"
                    for p in result.observation.files
                ),
                1,
            )
        self.assertFalse(path.exists())

    def test_conflicting_shared_package_files_fail_and_cleanup(self):
        for name, value in (("example", b"one"), ("helper", b"two")):
            self.fixture.replace_wheel_files(name, {"shared/__init__.py": value})
        with self.assertRaises(DependencyObservationError), self.install():
            self.fail("Conflicting shared file must not be admitted")
        self.assertEqual(
            sorted(p.name for p in self.fixture.root.iterdir()), ["installed"]
        )

    def test_console_gui_rewritten_and_obsolete_scripts(self):
        self.fixture.replace_wheel_files(
            "example",
            {
                "example-1.0.dist-info/entry_points.txt": (
                    b"[console_scripts]\nexample-cli = example:main\n"
                    b"[gui_scripts]\nexample-gui = example:main\n"
                ),
                "example-1.0.data/scripts/example-cli": b"obsolete setuptools wrapper",
                "example-1.0.data/scripts/custom-tool": (
                    b"#!python\nraise AssertionError('never execute')\n"
                ),
                "example/__init__.py": (
                    b"raise AssertionError('never import during installation')\n"
                ),
                "example/native.dll": b"MZ native fixture",
            },
        )
        with self.install() as result:
            changes = result.changes["example"]
            self.assertEqual(changes.skipped, ("example-1.0.data/scripts/example-cli",))
            self.assertEqual(len(changes.replaced), 1)
            self.assertEqual(len(changes.generated), 2)
            expected = "example-cli.exe" if os.name == "nt" else "example-cli"
            self.assertTrue((result.directory / "scripts" / expected).is_file())
            self.assertTrue(
                (result.directory / "scripts/custom-tool")
                .read_bytes()
                .startswith(b"#!" + sys.executable.encode())
            )
            result.revalidate()

    def test_installed_wrapper_tampering_is_rejected(self):
        self.fixture.replace_wheel_files(
            "example",
            {
                "example-1.0.dist-info/entry_points.txt": (
                    b"[console_scripts]\nexample-cli = example:main\n"
                )
            },
        )
        with self.install() as result:
            wrapper = result.changes["example"].generated[0]
            (result.directory / wrapper.path).write_bytes(b"altered wrapper")
            with self.assertRaises(DependencyObservationError):
                result.revalidate()

    def test_exact_installer_hash_is_required(self):
        with (
            self.assertRaises(DependencyObservationError),
            self.install(io.BytesIO(b"not the pinned pip wheel")),
        ):
            self.fail("Wrong installer must not run")
        self.assertEqual(
            sorted(p.name for p in self.fixture.root.iterdir()), ["installed"]
        )

    def test_unsafe_entrypoint_rejected_before_installer_process(self):
        self.fixture.replace_wheel_files(
            "example",
            {
                "example-1.0.dist-info/entry_points.txt": (
                    b"[console_scripts]\n../escape = example:main\n"
                )
            },
        )
        with patch(
            "literate_ai.adapters.dependencies.python_install.run_bounded_process"
        ) as process:
            with self.assertRaises(DependencyObservationError), self.install():
                self.fail("Unsafe entrypoint must not run")
            process.assert_not_called()

    def test_script_collision_between_distributions_is_rejected(self):
        declaration = b"[console_scripts]\nshared-tool = example:main\n"
        self.fixture.replace_wheel_files(
            "example", {"example-1.0.dist-info/entry_points.txt": declaration}
        )
        self.fixture.replace_wheel_files(
            "helper", {"helper-1.0.dist-info/entry_points.txt": declaration}
        )
        with self.assertRaises(DependencyObservationError), self.install():
            self.fail("Two distributions must not own one script")

    def test_consumer_failure_cleans_owned_environment(self):
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            with self.install() as result:
                path = result.directory
                raise RuntimeError("consumer failed")
        self.assertFalse(path.exists())

    def test_repeated_installs_have_identical_payload_evidence(self):
        self.fixture.replace_wheel_files(
            "example",
            {
                "example-1.0.dist-info/entry_points.txt": (
                    b"[console_scripts]\nexample-cli = example:main\n"
                )
            },
        )
        with self.install() as first:
            identity = first.observation.tree_identity
            process_identity = first.install_process_identity
        with self.install() as second:
            self.assertEqual(identity, second.observation.tree_identity)
            self.assertEqual(process_identity, second.install_process_identity)

    def test_failed_installer_process_cleans_partial_output(self):
        from literate_ai.adapters.builders._process import BoundedProcessResult

        with (
            patch(
                "literate_ai.adapters.dependencies.python_install.run_bounded_process",
                return_value=BoundedProcessResult(1, b"", b"failed"),
            ),
            self.assertRaises(DependencyObservationError),
            self.install(),
        ):
            self.fail("Failed installer must not yield an environment")
        self.assertEqual(
            sorted(p.name for p in self.fixture.root.iterdir()), ["installed"]
        )

if __name__ == "__main__":
    unittest.main()

