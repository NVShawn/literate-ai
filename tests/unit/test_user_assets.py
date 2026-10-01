from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_action_execution_config_path,
    resolve_test_config_path,
    resolve_worker_config_path,
    resolve_worker_observations_path,
)
from literate_ai.adapters.user_paths import UserPaths


class UserAssetPathTests(unittest.TestCase):
    def test_action_execution_uses_user_config_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            paths = UserPaths(root / "config", root / "state")
            self.assertEqual(
                resolve_action_execution_config_path(environment={}, paths=paths),
                root / "config" / "action-execution.json",
            )
            selected = root / "selected.json"
            self.assertEqual(
                resolve_action_execution_config_path(
                    environment={"LITAI_ACTION_EXECUTION_CONFIG": str(selected)},
                    paths=paths,
                ),
                selected,
            )
            self.assertFalse(selected.exists())

    def setUp(self) -> None:
        self.repository = Path(__file__).resolve().parents[2]

    def test_global_config_and_state_defaults_come_from_user_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = UserPaths(Path("/private/config"), Path("/private/state"))
            project = Path(temporary)
            self.assertEqual(
                resolve_worker_config_path(
                    environment={}, paths=paths, project_root=project
                ),
                Path("/private/config/workers.json"),
            )
            self.assertEqual(
                resolve_worker_observations_path(
                    environment={}, paths=paths, project_root=project
                ),
                Path("/private/state/worker-observations.json"),
            )

    def test_explicit_and_environment_overrides_must_be_absolute(self) -> None:
        for resolver, name in (
            (resolve_action_execution_config_path, "LITAI_ACTION_EXECUTION_CONFIG"),
            (resolve_worker_config_path, "LITAI_WORKER_CONFIG"),
            (resolve_worker_observations_path, "LITAI_WORKER_OBSERVATIONS"),
            (resolve_test_config_path, "LITAI_TEST_CONFIG"),
        ):
            with self.subTest(environment=name):
                with self.assertRaises(UserAssetPathError) as caught:
                    resolver(environment={name: "relative.json"})
                self.assertEqual(caught.exception.code, "user_assets.override_relative")

    def test_project_test_config_is_keyed_by_the_canonical_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            (project / "literate.project.json").write_bytes(
                (self.repository / "literate.project.json").read_bytes()
            )
            paths = UserPaths(Path(temporary) / "config", Path(temporary) / "state")

            selected = resolve_test_config_path(
                project_root=project, environment={}, paths=paths
            )

            self.assertEqual(
                selected, paths.config_root / "projects" / "literate-ai" / "test.json"
            )

    def test_unmigrated_legacy_default_fails_with_recovery_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            (project / "literate.workers.json").write_text("{}", encoding="utf-8")
            paths = UserPaths(Path(temporary) / "config", Path(temporary) / "state")

            with self.assertRaises(UserAssetPathError) as caught:
                resolve_worker_config_path(
                    project_root=project, environment={}, paths=paths
                )

            self.assertEqual(caught.exception.code, "user_assets.migration_required")
            self.assertIn("litai config migrate --apply", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
