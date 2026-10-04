from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.execution_dispatch import load_execution_worker_catalog
from literate_ai.adapters.test_matrix_config import load_global_test_matrix
from literate_ai.adapters.user_assets import (
    resolve_test_config_path,
    resolve_worker_config_path,
)
from literate_ai.adapters.user_config import load_user_mcp_catalog
from literate_ai.cli.dispatch import main
from literate_ai.contracts.channel_events import ChannelEvent, ChannelKind, ChannelRole


class ConfigMigrationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Path(__file__).resolve().parents[2]

    def _project(self, root: Path) -> Path:
        project = root / "project"
        project.mkdir()
        for name in (
            "literate.project.json",
            "literate.workers.example.json",
        ):
            source = self.repository / name
            destination = project / (
                "literate.workers.json"
                if name == "literate.workers.example.json"
                else name
            )
            destination.write_bytes(source.read_bytes())
        return project

    def _invoke(
        self, environment: dict[str, str], *arguments: str
    ) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", environment, clear=True):
            status = main(arguments, stdout=stdout, stderr=stderr)
        return status, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_complete_08_consumer_flow_resolves_only_migrated_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root)
            (project / "literate.test.json").write_bytes(
                (self.repository / "literate.test.example.json").read_bytes()
            )
            legacy_user = root / "home/.config/litai"
            (legacy_user / "events").mkdir(parents=True)
            (legacy_user / "mcps.json").write_text(
                json.dumps(
                    {
                        "schema": "urn:literate-ai:schema:v1:user-mcp-catalog",
                        "mcps": [{"id": "jira", "uses": "jira"}],
                    }
                ),
                encoding="utf-8",
            )
            event = ChannelEvent(
                ChannelRole.AUTHOR,
                ChannelKind.MUTAGENIC_CLI,
                "literate-ai",
                command="release.plan",
                outcome="passed",
            )
            (legacy_user / "events/1.json").write_text(
                json.dumps(event.to_dict()), encoding="utf-8"
            )
            environment = {
                "HOME": str(root / "home"),
                "USERPROFILE": str(root / "home"),
                "LITAI_CONFIG_DIR": str(root / "config"),
                "LITAI_STATE_DIR": str(root / "state"),
            }

            status, envelope, stderr = self._invoke(
                environment, "config", "migrate", str(project), "--apply"
            )
            self.assertEqual((status, stderr), (0, ""))
            self.assertTrue(envelope["result"]["ok"])
            with mock.patch.dict("os.environ", environment, clear=True):
                worker_path = resolve_worker_config_path(project_root=project)
                test_path = resolve_test_config_path(project_root=project)
                workers = load_execution_worker_catalog(worker_path)
                matrix = load_global_test_matrix(test_path)
                mcps = load_user_mcp_catalog()
            self.assertGreater(len(workers.workers), 0)
            self.assertGreater(len(matrix.workers), 0)
            self.assertTrue(mcps.has(mcps.mcps[0].uses))
            self.assertTrue((root / "state/events/1.json").is_file())
            self.assertFalse((project / "literate.workers.json").exists())
            self.assertFalse((project / "literate.test.json").exists())


if __name__ == "__main__":
    unittest.main()
