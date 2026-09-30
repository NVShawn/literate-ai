from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.test_matrix_config import load_global_test_matrix
from literate_ai.adapters.user_config_migration import (
    UserConfigMigrationError,
    apply_user_config_migration,
    plan_user_config_migration,
)
from literate_ai.adapters.user_paths import UserPaths
from literate_ai.contracts import WorkerHardwareObservationCatalog
from literate_ai.contracts.channel_events import ChannelEvent, ChannelKind, ChannelRole


class UserConfigMigrationTests(unittest.TestCase):
    def test_relative_legacy_home_retains_the_migration_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, _home, paths = self._layout(temporary)

            with self.assertRaises(UserConfigMigrationError) as caught:
                plan_user_config_migration(
                    project,
                    paths=paths,
                    legacy_home=Path("relative"),
                )

        self.assertEqual(caught.exception.code, "user_config.migration_home_invalid")

    def setUp(self) -> None:
        self.repository = Path(__file__).resolve().parents[2]

    def _layout(self, temporary: str) -> tuple[Path, Path, UserPaths]:
        base = Path(temporary)
        project = base / "project"
        home = base / "home"
        project.mkdir()
        home.mkdir()
        (project / "literate.project.json").write_bytes(
            (self.repository / "literate.project.json").read_bytes()
        )
        return project, home, UserPaths(base / "new-config", base / "new-state")

    def _legacy_assets(self, project: Path, home: Path) -> None:
        (project / "literate.workers.json").write_bytes(
            (self.repository / "literate.workers.example.json").read_bytes()
        )
        (project / "literate.test.json").write_bytes(
            (self.repository / "literate.test.example.json").read_bytes()
        )
        (project / "literate.worker-observations.json").write_text(
            json.dumps(WorkerHardwareObservationCatalog(()).to_dict()) + "\n",
            encoding="utf-8",
        )
        old_config = home / ".config" / "litai"
        events = old_config / "events"
        events.mkdir(parents=True)
        (old_config / "mcps.json").write_text(
            json.dumps(
                {
                    "schema": "urn:literate-ai:schema:v1:user-mcp-catalog",
                    "mcps": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        event = ChannelEvent(
            role=ChannelRole.AUTHOR,
            kind=ChannelKind.MUTAGENIC_CLI,
            project_id="literate-ai",
            command="generate",
        )
        (events / "1.json").write_text(
            json.dumps(event.to_dict()) + "\n", encoding="utf-8"
        )

    def test_dry_run_validates_every_asset_without_writing_or_disclosing_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, home, paths = self._layout(temporary)
            self._legacy_assets(project, home)

            plan = plan_user_config_migration(project, paths=paths, legacy_home=home)
            result = plan.to_dict(applied=False)

            self.assertEqual(len(plan.entries), 5)
            self.assertEqual({item.status for item in plan.entries}, {"ready"})
            self.assertFalse(Path(paths.config_root).exists())
            self.assertFalse(Path(paths.state_root).exists())
            rendered = json.dumps(result)
            self.assertNotIn("linux.example.invalid", rendered)
            self.assertNotIn("user@", rendered)

    def test_apply_moves_valid_assets_to_private_config_and_state_custody(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, home, paths = self._layout(temporary)
            self._legacy_assets(project, home)
            plan = plan_user_config_migration(project, paths=paths, legacy_home=home)

            result, status = apply_user_config_migration(plan)

            self.assertEqual(status, 0)
            self.assertTrue(result["ok"])
            self.assertEqual({item["status"] for item in result["entries"]}, {"moved"})
            self.assertTrue(Path(paths.worker_config).is_file())
            self.assertTrue(Path(paths.worker_observations).is_file())
            test_config = Path(paths.project_test_config("literate-ai"))
            self.assertTrue(test_config.is_file())
            load_global_test_matrix(test_config)
            for legacy in (
                project / "literate.workers.json",
                project / "literate.test.json",
                project / "literate.worker-observations.json",
                home / ".config" / "litai" / "mcps.json",
                home / ".config" / "litai" / "events" / "1.json",
            ):
                self.assertFalse(legacy.exists())
            if os.name != "nt":
                for target in (
                    Path(paths.worker_config),
                    Path(paths.worker_observations),
                    test_config,
                    Path(paths.mcp_catalog),
                    Path(paths.channel_events) / "1.json",
                ):
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_differing_destination_blocks_the_whole_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, home, paths = self._layout(temporary)
            self._legacy_assets(project, home)
            destination = Path(paths.worker_config)
            destination.parent.mkdir(parents=True)
            destination.write_text("different\n", encoding="utf-8")

            plan = plan_user_config_migration(project, paths=paths, legacy_home=home)
            result, status = apply_user_config_migration(plan)

            self.assertEqual(status, 1)
            self.assertFalse(result["ok"])
            self.assertIn("conflict", {item.status for item in plan.entries})
            self.assertTrue((project / "literate.workers.json").is_file())
            self.assertTrue((project / "literate.test.json").is_file())

    def test_identical_destination_is_idempotent_and_removes_legacy_duplicate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, home, paths = self._layout(temporary)
            source = project / "literate.workers.json"
            source.write_bytes(
                (self.repository / "literate.workers.example.json").read_bytes()
            )
            destination = Path(paths.worker_config)
            destination.parent.mkdir(parents=True)
            destination.write_bytes(source.read_bytes())

            plan = plan_user_config_migration(project, paths=paths, legacy_home=home)
            result, status = apply_user_config_migration(plan)

            self.assertEqual(status, 0)
            self.assertEqual(result["entries"][0]["status"], "removed-identical")
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_file())

    @unittest.skipIf(os.name == "nt", "symlink setup is not portable on Windows CI")
    def test_symlink_source_is_reported_invalid_and_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, home, paths = self._layout(temporary)
            outside = Path(temporary) / "outside.json"
            outside.write_bytes(
                (self.repository / "literate.workers.example.json").read_bytes()
            )
            (project / "literate.workers.json").symlink_to(outside)

            plan = plan_user_config_migration(project, paths=paths, legacy_home=home)
            result, status = apply_user_config_migration(plan)

            self.assertEqual(status, 1)
            self.assertEqual(plan.entries[0].status, "invalid")
            self.assertFalse(result["ok"])
            self.assertTrue(outside.is_file())
            self.assertFalse(Path(paths.worker_config).exists())

    def test_partial_publish_failure_keeps_the_unmoved_source_and_reports_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, home, paths = self._layout(temporary)
            worker_source = project / "literate.workers.json"
            worker_source.write_bytes(
                (self.repository / "literate.workers.example.json").read_bytes()
            )
            observation_source = project / "literate.worker-observations.json"
            observation_source.write_text(
                json.dumps(WorkerHardwareObservationCatalog(()).to_dict()) + "\n",
                encoding="utf-8",
            )
            plan = plan_user_config_migration(project, paths=paths, legacy_home=home)
            real_link = os.link
            calls = 0

            def fail_second_link(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publish failure")
                return real_link(*args, **kwargs)

            with mock.patch(
                "literate_ai.adapters.user_config_migration.os.link",
                side_effect=fail_second_link,
            ):
                result, status = apply_user_config_migration(plan)

            self.assertEqual(status, 1)
            self.assertFalse(result["ok"])
            self.assertFalse(worker_source.exists())
            self.assertTrue(Path(paths.worker_config).is_file())
            self.assertTrue(observation_source.is_file())
            self.assertFalse(Path(paths.worker_observations).exists())
            self.assertEqual(result["entries"][1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
