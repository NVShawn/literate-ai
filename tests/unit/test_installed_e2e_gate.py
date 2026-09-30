"""Contract tests for the installed-CLI end-to-end sentinel."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.installed_e2e_gate import SENTINEL_SCHEMA, _run_workspace, read_sentinel


class InstalledE2eGateTests(unittest.TestCase):
    def test_pre_export_binding_sentinel_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "installed-e2e.json"
            sentinel.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/installed-e2e-sentinel@1",
                        "cli_surface_identity": "sha256:unproved-working-tree",
                        "cli_surface_files": 17,
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(read_sentinel(sentinel))

    def test_dirty_checkout_records_only_the_exported_head_identity(self) -> None:
        completed = (
            subprocess.CompletedProcess((), 0, stdout="deadbeef\n", stderr=""),
            subprocess.CompletedProcess((), 0, stdout="", stderr=""),
            subprocess.CompletedProcess((), 0),
            subprocess.CompletedProcess((), 0),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            sentinel = root / "state" / "installed-e2e.json"
            with (
                mock.patch(
                    "scripts.installed_e2e_gate.cli_surface_identity",
                    return_value=("sha256:proved-head", 17),
                ),
                mock.patch(
                    "scripts.installed_e2e_gate.subprocess.run",
                    side_effect=completed,
                ) as run,
            ):
                status = _run_workspace(
                    repository=root,
                    workspace=workspace,
                    sentinel_path=sentinel,
                    working_tree_identity="sha256:unproved-working-tree",
                )

            recorded = json.loads(sentinel.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(recorded["schema"], SENTINEL_SCHEMA)
        self.assertEqual(recorded["cli_surface_identity"], "sha256:proved-head")
        self.assertEqual(recorded["cli_surface_files"], 17)


if __name__ == "__main__":
    unittest.main()
