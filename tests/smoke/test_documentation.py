"""Documentation graph smoke tests against real and minimal projects."""

from __future__ import annotations

import py_compile
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from literate_ai.documentation import (
    DocumentationError,
    extract_markdown_links,
    validate_documentation_graph,
)
from literate_ai.projects import ProjectError, documentation_files

ACTIVE_WORK = (
    "# Active work\n\n"
    "## P0\n\n"
    "### [ ] PROGRAM-001 — Deliver the program\n\n"
    "- **Evidence:** pending\n"
)


class DocumentationTests(unittest.TestCase):
    def _cache_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        docs = root / "docs"
        docs.mkdir()
        (root / "SKILL.md").write_text("Read [guide](docs/README.md).\n")
        (docs / "README.md").write_text("# Guide\n\n[Helper](helper.py)\n")
        helper = docs / "helper.py"
        helper.write_text("VALUE = 1\n")
        cache = docs / "__pycache__"
        cache.mkdir()
        bytecode = cache / f"helper.{sys.implementation.cache_tag}.pyc"
        py_compile.compile(str(helper), cfile=str(bytecode), doraise=True)
        return root, bytecode

    def _inventory_graph(self, root):
        project = SimpleNamespace(root=root, roots=lambda _: (root / "docs",))
        paths = documentation_files(project)
        documents, assets = {}, {}
        for path in paths:
            relative = path.relative_to(root).as_posix()
            (documents if path.suffix == ".md" else assets)[relative] = (
                path.read_bytes()
            )
        return validate_documentation_graph(
            project_root=root,
            agent_skill=root / "SKILL.md",
            documents=documents,
            assets=assets,
        )

    def test_real_bytecode_and_atomic_cache_writes_do_not_change_document_authority(
        self,
    ):
        root, bytecode = self._cache_fixture()
        before = self._inventory_graph(root)
        bytecode.write_bytes(b"changed cache contents")
        bytecode.with_name(bytecode.name + ".12345").write_bytes(b"partial cache")
        (bytecode.parent / "helper.pyo").write_bytes(b"legacy cache")
        self.assertEqual(before, self._inventory_graph(root))
        self.assertEqual(
            [asset["path"] for asset in before["assets"]], ["docs/helper.py"]
        )

    def test_real_assets_remain_required_even_inside_cache_directories(self):
        for relative in (
            "image.png",
            "__pycache__/image.png",
            "helper.pyc",
            "__pycache__/helper.pyc.notes",
        ):
            with self.subTest(relative=relative):
                root, _ = self._cache_fixture()
                (root / "docs" / relative).write_bytes(b"authored asset")
                with self.assertRaises(DocumentationError) as refused:
                    self._inventory_graph(root)
                self.assertEqual(
                    refused.exception.code, "project.documentation_asset_unreferenced"
                )

    def test_explicit_bytecode_links_are_rejected_with_source_and_target(self):
        root, bytecode = self._cache_fixture()
        guide = root / "docs/README.md"
        guide.write_text(
            guide.read_text() + f"\n[Cache](__pycache__/{bytecode.name})\n"
        )
        with self.assertRaises(DocumentationError) as refused:
            self._inventory_graph(root)
        self.assertEqual(
            refused.exception.code, "project.documentation_link_generated_cache"
        )
        self.assertIn("docs/README.md:5", refused.exception.message)
        self.assertIn(bytecode.name, refused.exception.message)

    def test_cache_files_cannot_bypass_existing_symlink_refusal(self):
        from unittest.mock import patch

        root, bytecode = self._cache_fixture()
        original = Path.is_symlink

        def is_symlink(path):
            return path == bytecode or original(path)

        with patch.object(Path, "is_symlink", is_symlink):
            with self.assertRaises(ProjectError) as refused:
                self._inventory_graph(root)
        self.assertEqual(refused.exception.code, "project.documentation_invalid")

    def test_repository_readme_links_only_to_tracked_existing_documents(self) -> None:
        """GitHub-facing root links cannot be satisfied only by package templates."""

        repository = Path(__file__).resolve().parents[2]
        readme = (repository / "README.md").read_text(encoding="utf-8")
        local_links = tuple(
            link
            for link in extract_markdown_links(readme)
            if not link.image and not urlsplit(link.destination).scheme
        )
        missing = []
        for link in local_links:
            destination = unquote(urlsplit(link.destination).path)
            if not destination or destination.startswith("#"):
                continue
            target = repository.joinpath(*Path(destination).parts)
            if not target.exists() or target.is_symlink():
                missing.append(destination)
        self.assertEqual(missing, [])

    def test_documentation_graph_applies_lifecycle_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = {
                "docs/README.md": ("# Guide\n\n[Work](roadmap/active-work.md)\n"),
                "docs/roadmap/active-work.md": (
                    ACTIVE_WORK + "\n[Program](program.md)\n"
                ),
                "docs/roadmap/program.md": "# Program without a lifecycle header\n",
            }
            for relative, content in documents.items():
                target = root.joinpath(*Path(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            skill = root / "SKILL.md"
            skill.write_text("Read the [guide](docs/README.md).\n", encoding="utf-8")

            with self.assertRaises(DocumentationError) as raised:
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents={
                        path: content.encode() for path, content in documents.items()
                    },
                    assets={},
                )

        self.assertEqual(
            raised.exception.code,
            "project.roadmap_lifecycle_header_missing",
        )

    def test_custom_onboarding_inside_documentation_is_reachable_but_not_its_own_spine(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "docs/onboarding.md"
            skill.parent.mkdir()
            guide = root / "docs/README.md"
            guide.write_text("# Guide\n")
            content = "# Onboarding\n\nRead [guide](README.md).\n"
            skill.write_text(content)
            graph = validate_documentation_graph(
                project_root=root,
                agent_skill=skill,
                documents={
                    "docs/onboarding.md": content.encode(),
                    "docs/README.md": guide.read_bytes(),
                },
                assets={},
            )
            self.assertEqual(graph["entrypoint"], "docs/onboarding.md")
            self.assertEqual(
                graph["reachable_documents"], ["docs/README.md", "docs/onboarding.md"]
            )
            skill.write_text("# Onboarding\n\n[Self](onboarding.md)\n")
            with self.assertRaises(DocumentationError) as refused:
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents={"docs/onboarding.md": skill.read_bytes()},
                    assets={},
                )
            self.assertEqual(
                refused.exception.code, "project.agent_skill_documentation_missing"
            )


if __name__ == "__main__":
    unittest.main()
