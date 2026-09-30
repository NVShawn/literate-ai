from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.harness_workspace import (
    HarnessWorkspaceError,
    harness_workspace_link_evidence,
    harness_workspace_runtime_identity,
    materialize_harness_workspace_links,
    require_harness_workspace_link_evidence,
    resolve_harness_workspace_links,
)


class HarnessWorkspaceTests(unittest.TestCase):
    def test_resolves_sorted_external_siblings_without_disclosing_host_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            first = root / "first-sdk"
            second = root / "second-sdk"
            for path in (project, first, second):
                path.mkdir()

            links = resolve_harness_workspace_links(
                ("z-sdk=../second-sdk", "a-sdk=../first-sdk"),
                base=project,
                project_root=project,
            )

            self.assertEqual([item.destination for item in links], ["a-sdk", "z-sdk"])
            evidence = harness_workspace_link_evidence(links)
            self.assertEqual(evidence["destinations"], ["a-sdk", "z-sdk"])
            self.assertNotIn(str(root), json.dumps(evidence))
            self.assertTrue(
                harness_workspace_runtime_identity(links).uri.startswith("sha256:")
            )

    def test_rejects_invalid_duplicate_missing_unsafe_and_overlapping_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            external = root / "external"
            project.mkdir()
            external.mkdir()
            cases = (
                ("missing-separator",),
                ("../escape=external",),
                ("legacy=external",),
                ("sdk=",),
                ("sdk=missing",),
                ("sdk=external", "sdk=external"),
                (f"sdk={project}",),
                (f"sdk={root}",),
            )
            for values in cases:
                with (
                    self.subTest(values=values),
                    self.assertRaises(HarnessWorkspaceError),
                ):
                    resolve_harness_workspace_links(
                        values, base=root, project_root=project
                    )

            alias = root / "external-alias"
            try:
                alias.symlink_to(external, target_is_directory=True)
            except OSError:
                return
            with self.assertRaisesRegex(HarnessWorkspaceError, "safe directory"):
                resolve_harness_workspace_links(
                    (f"sdk={alias}",), base=root, project_root=project
                )

    def test_materializes_and_removes_directory_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            external = root / "external"
            workspace = root / "workspace"
            for path in (project, external, workspace):
                path.mkdir()
            (external / "sentinel").write_text("ready\n", encoding="utf-8")
            links = resolve_harness_workspace_links(
                (f"sdk={external}",), base=root, project_root=project
            )

            with materialize_harness_workspace_links(workspace, links):
                self.assertEqual(
                    (workspace / "sdk" / "sentinel").read_text(encoding="utf-8"),
                    "ready\n",
                )
            self.assertFalse((workspace / "sdk").exists())
            self.assertFalse((workspace / "sdk").is_symlink())

    @unittest.skipIf(os.name == "nt", "unlink replacement mechanics differ on Windows")
    def test_detects_link_replacement_after_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            external = root / "external"
            replacement = root / "replacement"
            workspace = root / "workspace"
            for path in (project, external, replacement, workspace):
                path.mkdir()
            links = resolve_harness_workspace_links(
                (f"sdk={external}",), base=root, project_root=project
            )

            with self.assertRaisesRegex(HarnessWorkspaceError, "changed"):
                with materialize_harness_workspace_links(workspace, links):
                    (workspace / "sdk").unlink()
                    (workspace / "sdk").symlink_to(
                        replacement, target_is_directory=True
                    )

    def test_runtime_must_supply_exact_qualified_destination_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            external = root / "external"
            project.mkdir()
            external.mkdir()
            links = resolve_harness_workspace_links(
                (f"sdk={external}",), base=root, project_root=project
            )

            require_harness_workspace_link_evidence(
                harness_workspace_link_evidence(links), links
            )
            with self.assertRaisesRegex(HarnessWorkspaceError, "every and only"):
                require_harness_workspace_link_evidence(None, links)


if __name__ == "__main__":
    unittest.main()
