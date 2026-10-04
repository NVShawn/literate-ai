"""Focused tests for the public external lifecycle-driver adapter."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
