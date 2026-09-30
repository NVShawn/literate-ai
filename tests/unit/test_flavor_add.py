"""Adding a Flavor to an existing project does not re-run init."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.flavor_add import FlavorAddError, add_flavor_to_project
from literate_ai.adapters.project_initialization import initialize_project
from literate_ai.contracts import RepositoryParentSelection
from tests.unit.test_project_cli import invoke


class FlavorAddTests(unittest.TestCase):
    def test_macos_project_can_gain_linux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+javascript", "+macos"),
            )
            (target / "samples" / "hello" / "component.md").parent.mkdir(parents=True)
            (target / "samples" / "hello" / "component.md").write_text(
                "# hello\n", encoding="utf-8"
            )
            (target / "samples" / "hello" / "component.lock.json").write_text(
                "{}\n", encoding="utf-8"
            )
            result = add_flavor_to_project(target, "linux")
            self.assertEqual(result["state"], "completed")
            self.assertTrue((target / "flavors" / "os-linux" / "flavor.md").is_file())
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "+flavor://literate-ai/os-linux", manifest["default_flavor_selectors"]
            )
            self.assertFalse(
                (target / "samples" / "hello" / "component.lock.json").exists()
            )
            self.assertTrue((target / "samples" / "hello" / "component.md").is_file())

    def test_python_and_go_fail_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )
            before = (target / "literate.project.json").read_bytes()
            with self.assertRaises(FlavorAddError) as raised:
                add_flavor_to_project(target, "go")
            self.assertEqual(raised.exception.code, "flavor.add_conflict")
            self.assertEqual(before, (target / "literate.project.json").read_bytes())
            self.assertFalse((target / "flavors" / "go").exists())

    def test_no_default_installs_files_without_conflict_or_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )
            before = (target / "literate.project.json").read_bytes()
            result = add_flavor_to_project(target, "javascript", set_default=False)
            self.assertEqual(result["state"], "completed")
            self.assertFalse(result["default"])
            self.assertTrue(
                (target / "flavors" / "lang-javascript" / "flavor.md").is_file()
            )
            # Project manifest unchanged — no default added.
            self.assertEqual(before, (target / "literate.project.json").read_bytes())
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "+flavor://literate-ai/lang-javascript",
                manifest.get("default_flavor_selectors", []),
            )

    def test_add_is_idempotent_when_already_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )
            first = add_flavor_to_project(target, "linux")
            second = add_flavor_to_project(target, "os-linux")
            self.assertEqual(first["state"], "completed")
            self.assertEqual(second["state"], "unchanged")

    def test_add_rejects_manifest_changed_after_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )
            manifest = target / "literate.project.json"
            original = manifest.read_bytes()

            def concurrent_stamp(root, directory, *, overwrite):
                manifest.write_bytes(original + b" ")
                return []

            with mock.patch(
                "literate_ai.adapters.flavor_add._stamp_flavor_files",
                side_effect=concurrent_stamp,
            ):
                with self.assertRaises(FlavorAddError) as raised:
                    add_flavor_to_project(target, "linux")
            self.assertEqual(raised.exception.code, "project.manifest_stale")
            self.assertEqual(manifest.read_bytes(), original + b" ")

    def test_cli_add_docker_stamps_flavor_and_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )
            status, envelope = invoke(
                "--json", "flavor", "add", "docker", "--project", str(target)
            )
            self.assertEqual(status, 0)
            self.assertEqual(envelope["ok"], True)
            self.assertTrue(
                (target / "flavors" / "deploy-docker" / "flavor.md").is_file()
            )
            self.assertTrue(
                (
                    target
                    / "skills"
                    / "specification-to-source"
                    / "docker-container-application"
                    / "SKILL.md"
                ).is_file()
            )

    def test_cli_add_package_npm_stamps_flavor_and_ecosystem_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+python", "+macos"),
            )
            status, envelope = invoke(
                "--json", "flavor", "add", "package-npm", "--project", str(target)
            )
            self.assertEqual(status, 0)
            self.assertEqual(envelope["ok"], True)
            self.assertTrue(
                (target / "flavors" / "package-npm" / "flavor.md").is_file()
            )
            self.assertTrue(
                (
                    target
                    / "skills"
                    / "specification-to-source"
                    / "javascript-ecosystem"
                    / "SKILL.md"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
