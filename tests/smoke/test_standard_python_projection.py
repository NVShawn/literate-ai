"""Locked Python library wheel projection and pre-discovery conflict refusal."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.standard_project import (
    StandardCommandProjectionError,
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import ComponentCommandPhase
from tests.support.fixtures_test_standard_command_projection import (
    _locked_snapshot,
    _observation,
    _tool,
)


class StandardPythonProjectionTests(unittest.TestCase):
    def test_library_and_application_wheel_authority_on_each_os(self):
        for platform in ("linux", "windows", "macos"):
            for library in (False, True):
                with self.subTest(platform=platform, library=library):
                    with tempfile.TemporaryDirectory() as temporary:
                        _, snapshot, execution = _locked_snapshot(
                            Path(temporary),
                            platform=platform,
                            package_python=True,
                            no_entrypoint=library,
                        )
                        closure = project_locked_standard_toolchain_closure(
                            snapshot,
                            execution,
                            host_platform=platform,
                            toolchain_discoverer=lambda name, *_: _tool(name),
                            dependency_observer=_observation,
                        )
                        closure.require_unchanged()
                        target = closure.python_targets[0]
                        contract = closure.contracts[0]
                        self.assertEqual(contract.is_library, library)
                        self.assertEqual(
                            contract.locked_build_authority_identity, target.identity
                        )
                        self.assertEqual(
                            contract.language_runtime_identity,
                            target.python_toolchain_identity,
                        )
                        self.assertEqual(target.manifest, "source/requirements.txt")
                        self.assertEqual(
                            target.lockfile, "source/python-wheel-lock.json"
                        )
                        for phase in ComponentCommandPhase:
                            self.assertEqual(
                                contract.command(phase).argv[:5],
                                ("{tool}", "-I", "-S", "-B", "-c"),
                            )
                        if library:
                            self.assertEqual(
                                contract.library_import_surface.language, "python"
                            )

    def test_competing_profiles_and_multiple_entrypoints_fail_before_discovery(self):
        for options in (
            {"build_system": "make", "no_entrypoint": True},
            {"duplicate_entrypoint": True},
        ):
            with (
                self.subTest(options=options),
                tempfile.TemporaryDirectory() as temporary,
            ):
                _, snapshot, execution = _locked_snapshot(
                    Path(temporary),
                    package_python=True,
                    **options,
                )
                discover = mock.Mock(
                    side_effect=AssertionError("tool discovery forbidden")
                )
                with self.assertRaises(StandardCommandProjectionError) as caught:
                    project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform="macos",
                        toolchain_discoverer=discover,
                        dependency_observer=_observation,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "standard_command.python_wheel_profile_unsupported",
                )
                discover.assert_not_called()
