"""Focused tests for the public external lifecycle-driver adapter."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.live_test_selection import configured_test_coding_cli
from literate_ai.adapters.models import CodingCliError
from literate_ai.adapters.project_lifecycle_driver import (
    ProjectLifecycleDriverAdapterError,
    bind_external_project_lifecycle_driver,
    lifecycle_driver_environment_identity_material,
    lifecycle_driver_implementation_identity,
)
from literate_ai.contracts import (
    MINIMUM_PROJECT_REBUILD_PHASES,
    ProjectLifecycleDriver,
    canonical_identity,
)


class ProjectLifecycleDriverAdapterTests(unittest.TestCase):
    def _binding(self, root: Path):
        implementation = root / "driver.py"
        implementation.write_text("print('driver')\n", encoding="utf-8")
        project = SimpleNamespace(root=root)
        provisional = ProjectLifecycleDriver(
            driver_id="fixture",
            version="1.0.0",
            implementation_paths=("driver.py",),
            implementation_identity=canonical_identity({"provisional": True}),
            argv=(
                "{python}",
                "driver.py",
                "{project}",
                "{specification}",
                "{runtime_root}",
                "{candidate_receipt}",
                "{project_revision_identity}",
                "{lifecycle_request_identity}",
                "{flavor_args}",
                "{allow_host_execution}",
            ),
            environment_keys=("PATH", "OPENAI_API_KEY"),
            phases=MINIMUM_PROJECT_REBUILD_PHASES,
            specification_scope="project",
        )
        identity = lifecycle_driver_implementation_identity(project, provisional)
        driver = ProjectLifecycleDriver(
            driver_id=provisional.driver_id,
            version=provisional.version,
            implementation_paths=provisional.implementation_paths,
            implementation_identity=identity,
            argv=provisional.argv,
            environment_keys=provisional.environment_keys,
            phases=provisional.phases,
            specification_scope=provisional.specification_scope,
        )
        return bind_external_project_lifecycle_driver(
            project,
            driver,
            {
                "CODING_CLI": "codex",
                "PATH": os.environ.get("PATH", ""),
                "OPENAI_API_KEY": "secret",
            },
        )

    def test_factory_rejects_implementation_drift_with_stable_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding = self._binding(root)
            (root / "driver.py").write_text("print('changed')\n", encoding="utf-8")

            with self.assertRaises(ProjectLifecycleDriverAdapterError) as raised:
                bind_external_project_lifecycle_driver(
                    binding.project,
                    binding.driver,
                    {"PATH": os.environ.get("PATH", "")},
                )

            self.assertEqual(
                raised.exception.code, "rebuild.driver_implementation_mismatch"
            )

    def test_environment_identity_never_binds_credential_values(self) -> None:
        first = lifecycle_driver_environment_identity_material(
            {
                "PATH": "/one",
                "CODEX_API_KEY": "secret-one",
                "LITAI_INHERITED_SESSION_AUTH_KEY": "aa" * 32,
            }
        )
        second = lifecycle_driver_environment_identity_material(
            {
                "PATH": "/one",
                "CODEX_API_KEY": "secret-two",
                "LITAI_INHERITED_SESSION_AUTH_KEY": "bb" * 32,
            }
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first["credential_key_presence"],
            ["CODEX_API_KEY", "LITAI_INHERITED_SESSION_AUTH_KEY"],
        )
        self.assertNotIn("secret-one", json.dumps(first))

    def test_driver_cli_defaults_to_the_configuration_the_driver_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding = self._binding(root)
            seen = []

            def configured(*, environment, project_root):
                seen.append(dict(environment))
                return "opencode"

            for source, expected in (
                ({"PATH": "/tools", "LITAI_TEST_CONFIG": "/other.json"}, "opencode"),
                ({"PATH": "/tools", "CODING_CLI": "codex"}, "codex"),
            ):
                with (
                    self.subTest(expected=expected),
                    patch(
                        "literate_ai.adapters.project_lifecycle_driver."
                        "configured_test_coding_cli",
                        configured,
                    ),
                ):
                    bound = bind_external_project_lifecycle_driver(
                        binding.project, binding.driver, source
                    )
                    self.assertEqual(bound.environment["CODING_CLI"], expected)
            # Only keys the driver receives choose its test configuration.
            self.assertEqual(seen, [{"PATH": "/tools"}])

    def test_configured_cli_comes_from_the_test_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "test.json"
            environment = {"LITAI_TEST_CONFIG": str(config)}
            self.assertIsNone(
                configured_test_coding_cli(environment=environment, project_root=None)
            )
            config.write_text(
                json.dumps({"coding_cli": "opencode", "model": "provider/model"})
            )
            self.assertEqual(
                configured_test_coding_cli(environment=environment, project_root=None),
                "opencode",
            )
            for content, code in (
                ("[]", "coding_cli.test_selection_invalid"),
                ('{"coding_cli": "Opencode"}', "coding_cli.unsupported"),
            ):
                with self.subTest(code=code):
                    config.write_text(content)
                    with self.assertRaises(CodingCliError) as raised:
                        configured_test_coding_cli(
                            environment=environment, project_root=None
                        )
                    self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
