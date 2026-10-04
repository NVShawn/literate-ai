"""Documentation graph smoke tests against real and minimal projects."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from literate_ai.documentation import (
    DocumentationError,
    extract_markdown_links,
    validate_documentation_graph,
)

ACTIVE_WORK = (
    "# Active work\n\n"
    "## P0\n\n"
    "### [ ] PROGRAM-001 — Deliver the program\n\n"
    "- **Evidence:** pending\n"
)


class DocumentationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
