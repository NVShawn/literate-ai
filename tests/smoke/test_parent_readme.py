"""Observable attribution behavior for arbitrary validated project parents."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.parent_readme import (
    ParentReadmeError,
    reconcile_parent_readme,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.contracts import (
    RepositoryLineage,
    RepositoryLineageNode,
    RepositoryParentReference,
    RepositoryParentSelection,
)
from tests.support.fixtures_test_repository_lineage import identity


def parents(root, *urls):
    refs = tuple(RepositoryParentReference(url, "main") for url in sorted(urls))
    selection = (
        RepositoryParentSelection.inherit(refs)
        if refs
        else RepositoryParentSelection.root()
    )
    nodes = tuple(
        RepositoryLineageNode(
            f"parent-{i}",
            identity(str(i + 1)),
            ref.repository_url,
            "main",
            "a" * 40,
            RepositoryParentSelection.root(),
            (),
        )
        for i, ref in enumerate(refs)
    )
    FilesystemRepositoryLineageStore(root).replace(
        selection,
        RepositoryLineage(
            selection,
            nodes,
            tuple(sorted((node.identity for node in nodes), key=lambda item: item.uri)),
        ),
    )


class ParentReadmeTests(unittest.TestCase):
    def test_initialized_instructions_route_successful_reparent_to_actual_direct_parent(
        self,
    ):
        from literate_ai.adapters.project_reparenting import (
            FilesystemRepositoryReparentAdapter,
        )
        from tests.smoke.test_project_reparenting import initialize_root
        from tests.support.fixtures_test_repository_lineage import fixture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            initialize_root(root)
            self.assertIn(
                "maintain-parent-readme/SKILL.md", (root / "SKILL.md").read_text()
            )
            self.assertTrue(
                (root / "skills/agent/maintain-parent-readme/SKILL.md").is_file()
            )
            self.assertFalse(reconcile_parent_readme(root)["changed"])
            selection, _, _, lineage = fixture()
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selected: lineage
            )
            plan = adapter.plan(root, selection)
            self.assertFalse((root / "README.md").exists())
            adapter.apply(root, plan)
            report = reconcile_parent_readme(root, apply=True)
            self.assertEqual(report["parent_urls"], ["https://example.test/child"])
            self.assertNotIn("example.test/root", (root / "README.md").read_text())

    def test_plan_create_apply_and_repeat_for_arbitrary_ssh_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents(root, "ssh://git@forge.example.test/team/custom.git")
            report = reconcile_parent_readme(root)
            self.assertTrue(report["changed"])
            self.assertFalse((root / "README.md").exists())
            self.assertTrue(reconcile_parent_readme(root, apply=True)["applied"])
            text = (root / "README.md").read_text()
            self.assertIn(
                "[forge.example.test/team/custom](<https://forge.example.test/team/custom>)",
                text,
            )
            before = (root / "README.md").stat().st_mtime_ns
            self.assertFalse(reconcile_parent_readme(root, apply=True)["changed"])
            self.assertEqual((root / "README.md").stat().st_mtime_ns, before)

    def test_reparent_preserves_authored_content_and_reconciles_multiple_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authored = b"# Authored title\r\n\r\nKeep this **exact** prose.\r\n"
            (root / "README.md").write_bytes(authored)
            parents(root, "https://first.test/team/project.git")
            reconcile_parent_readme(root, apply=True)
            parents(
                root,
                "https://second.test/nested/team/project.git",
                "git://third.test/project.git",
            )
            reconcile_parent_readme(root, apply=True)
            text = (root / "README.md").read_bytes()
            self.assertTrue(text.startswith(authored))
            self.assertNotIn(b"first.test", text)
            self.assertIn(b"https://second.test/nested/team/project", text)
            self.assertIn(b"https://third.test/project", text)
            self.assertFalse(reconcile_parent_readme(root)["changed"])
            parents(root)
            reconcile_parent_readme(root, apply=True)
            text = (root / "README.md").read_bytes()
            self.assertTrue(text.startswith(authored))
            self.assertNotIn(b"Parent repository:", text)

    def test_existing_authored_link_is_not_duplicated_when_other_parent_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authored = b"# Guide\n[Our base](https://first.test/base)\n"
            (root / "README.md").write_bytes(authored)
            parents(root, "https://first.test/base.git", "https://second.test/base.git")
            reconcile_parent_readme(root, apply=True)
            self.assertEqual(
                (root / "README.md").read_bytes().count(b"https://first.test/base"), 1
            )
            self.assertFalse(reconcile_parent_readme(root, apply=True)["changed"])
            parents(root, "https://first.test/base.git", "https://third.test/base.git")
            reconcile_parent_readme(root, apply=True)
            text = (root / "README.md").read_bytes()
            self.assertTrue(text.startswith(authored))
            self.assertEqual(text.count(b"https://first.test/base"), 1)
            self.assertNotIn(b"second.test", text)

    def test_example_links_do_not_count_as_clickable_attribution(self):
        for example in (
            "```md\n[Example](https://forge.test/base)\n```\n",
            "`[Example](https://forge.test/base)`\n",
            "<!-- [Example](https://forge.test/base) -->\n",
            "    [Example](https://forge.test/base)\n",
        ):
            with (
                self.subTest(example=example),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                parents(root, "https://forge.test/base.git")
                (root / "README.md").write_text(example)
                self.assertTrue(reconcile_parent_readme(root, apply=True)["changed"])
                self.assertIn("Parent repository:", (root / "README.md").read_text())
                self.assertFalse(reconcile_parent_readme(root)["changed"])

    def test_root_or_absent_lineage_never_invents_a_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(reconcile_parent_readme(root, apply=True)["changed"])
            parents(root)
            self.assertFalse(reconcile_parent_readme(root, apply=True)["changed"])
            self.assertFalse((root / "README.md").exists())

    def test_local_parent_requires_explicit_browser_evidence_and_cli_explains_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = (root / "parent").as_uri()
            parents(root, repository)
            with self.assertRaises(ParentReadmeError):
                reconcile_parent_readme(root, apply=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "literate_ai.adapters.parent_readme",
                    "--project",
                    str(root),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--browser-url", json.loads(result.stdout)["message"])
            report = reconcile_parent_readme(
                root,
                apply=True,
                browser_urls={repository: "https://forge.test/team/base"},
            )
            self.assertEqual(report["parent_urls"], ["https://forge.test/team/base"])
            self.assertNotIn(repository, (root / "README.md").read_text())

    def test_unsafe_readme_or_ambiguous_markers_are_refused_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents(root, "https://forge.test/base.git")
            target = root / "target"
            target.write_text("keep")
            readme = root / "README.md"
            if os.name != "nt":
                readme.symlink_to(target)
                with self.assertRaises(ParentReadmeError):
                    reconcile_parent_readme(root, apply=True)
                self.assertEqual(target.read_text(), "keep")
                readme.unlink()
            text = "<!-- literate-ai:parent-repositories -->\n"
            readme.write_text(text)
            with self.assertRaises(ParentReadmeError):
                reconcile_parent_readme(root, apply=True)
            self.assertEqual(readme.read_text(), text)
