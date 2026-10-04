"""Adding a Flavor to an existing project does not re-run init."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.flavor_add import FlavorAddError, add_flavor_to_project
from literate_ai.adapters.project_initialization import initialize_project
from literate_ai.contracts import RepositoryParentSelection


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


if __name__ == "__main__":
    unittest.main()
