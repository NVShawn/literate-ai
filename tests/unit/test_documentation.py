"""Deterministic documentation parser and graph tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from literate_ai.documentation import (
    DocumentationError,
    extract_markdown_links,
    markdown_anchors,
    validate_documentation_graph,
    validate_roadmap_lifecycle,
)


class MarkdownNavigationTests(unittest.TestCase):
    def test_inline_and_reference_links_preserve_kind_and_line(self) -> None:
        links = extract_markdown_links(
            "# Guide\n\n"
            "Read [inline](one.md), [full][two], [collapsed][], and [short].\n"
            "![asset](image.svg)\n\n"
            '[two]: <two.md#target> "title"\n'
            "[collapsed]: three.md\n"
            "[short]: four.md\n"
        )

        self.assertEqual(
            [(item.destination, item.line, item.image, item.syntax) for item in links],
            [
                ("one.md", 3, False, "inline"),
                ("two.md#target", 3, False, "reference"),
                ("three.md", 3, False, "reference"),
                ("four.md", 3, False, "shortcut"),
                ("image.svg", 4, True, "inline"),
            ],
        )

    def test_code_comments_and_non_navigation_html_are_ignored(self) -> None:
        links = extract_markdown_links(
            "`[inline](ignored.md)` and <!-- [comment](ignored.md) --> [ok](ok.md)\n"
            "```markdown\n[also ignored](ignored.md)\n```\n"
            "<details><summary>Fine</summary></details>\n"
        )
        self.assertEqual([item.destination for item in links], ["ok.md"])

    def test_raw_html_navigation_and_undefined_references_fail_closed(self) -> None:
        with self.assertRaisesRegex(DocumentationError, "raw HTML"):
            extract_markdown_links('<a href="guide.md">guide</a>\n')
        with self.assertRaisesRegex(DocumentationError, "no definition"):
            extract_markdown_links("Read [missing][reference].\n")

    def test_duplicate_heading_slugs_and_explicit_anchors_are_indexed(self) -> None:
        anchors = markdown_anchors(
            "# Hello, World!\n\n# Hello World\n\n"
            "Setext heading\n--------------\n"
            '<a id="chosen-anchor"></a>\n'
        )
        self.assertEqual(
            anchors,
            frozenset(
                {
                    "hello-world",
                    "hello-world-1",
                    "setext-heading",
                    "chosen-anchor",
                }
            ),
        )


class DocumentationGraphTests(unittest.TestCase):
    def _project(
        self,
        root: Path,
        *,
        skill: str,
        documents: dict[str, str],
        assets: dict[str, bytes] | None = None,
    ) -> tuple[Path, dict[str, bytes], dict[str, bytes]]:
        (root / "SKILL.md").write_text(skill, encoding="utf-8")
        encoded: dict[str, bytes] = {}
        for relative, content in documents.items():
            target = root.joinpath(*Path(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            encoded[relative] = content.encode()
        asset_bytes = assets or {}
        for relative, content in asset_bytes.items():
            target = root.joinpath(*Path(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return root / "SKILL.md", encoded, asset_bytes

    def test_every_document_and_asset_must_be_reachable_from_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill, documents, assets = self._project(
                root,
                skill="Read the [guide](docs/README.md).\n",
                documents={
                    "docs/README.md": (
                        "# Guide\n\nRead [details](details.md#details). "
                        "![map](map.svg)\n"
                    ),
                    "docs/details.md": "# Details\n",
                },
                assets={"docs/map.svg": b"<svg/>\n"},
            )

            report = validate_documentation_graph(
                project_root=root,
                agent_skill=skill,
                documents=documents,
                assets=assets,
            )

        self.assertEqual(
            report["reachable_documents"],
            ["docs/README.md", "docs/details.md"],
        )
        self.assertEqual(report["assets"][0]["path"], "docs/map.svg")

    def test_broken_fragment_or_orphan_document_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill, documents, assets = self._project(
                root,
                skill="Read [guide](docs/README.md).\n",
                documents={
                    "docs/README.md": "Read [details](details.md#missing).\n",
                    "docs/details.md": "# Details\n",
                },
            )
            with self.assertRaisesRegex(DocumentationError, "fragment"):
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents=documents,
                    assets=assets,
                )

            documents["docs/README.md"] = b"# Guide\n"
            (root / "docs" / "README.md").write_bytes(documents["docs/README.md"])
            with self.assertRaisesRegex(DocumentationError, "unreachable"):
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents=documents,
                    assets=assets,
                )

    def test_skill_cannot_use_reference_or_html_syntax_to_reach_ambient_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill, documents, assets = self._project(
                root,
                skill="Read [ambient][notes].\n\n[notes]: notes.md\n",
                documents={"docs/README.md": "# Guide\n"},
            )
            (root / "notes.md").write_text("# Ambient\n", encoding="utf-8")
            with self.assertRaisesRegex(DocumentationError, "undeclared"):
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents=documents,
                    assets=assets,
                )
            (root / "SKILL.md").write_text(
                '<a href="notes.md">ambient</a>\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(DocumentationError, "raw HTML"):
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=root / "SKILL.md",
                    documents=documents,
                    assets=assets,
                )

    def test_remote_images_and_local_symlink_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill, documents, assets = self._project(
                root,
                skill="Read [guide](docs/README.md).\n",
                documents={
                    "docs/README.md": "![remote](https://example.test/map.svg)\n"
                },
            )
            with self.assertRaisesRegex(DocumentationError, "not content-identified"):
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents=documents,
                    assets=assets,
                )

            (root / "docs" / "README.md").write_text(
                "Read [linked](linked.md).\n", encoding="utf-8"
            )
            documents["docs/README.md"] = b"Read [linked](linked.md).\n"
            try:
                (root / "docs" / "linked.md").symlink_to(root / "outside.md")
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            (root / "outside.md").write_text("# Outside\n", encoding="utf-8")
            with self.assertRaisesRegex(DocumentationError, "symbolic link"):
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents=documents,
                    assets=assets,
                )

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


class RoadmapLifecycleTests(unittest.TestCase):
    ACTIVE_WORK = (
        "# Active work\n\n"
        "## P0\n\n"
        "### [ ] PROGRAM-001 — Deliver the program\n\n"
        "- **Evidence:** pending\n"
    )
    OWNER = "[PROGRAM-001](active-work.md#program-001-deliver-the-program)"

    def _documents(self, plan: str) -> dict[str, str]:
        return {
            "docs/roadmap/active-work.md": self.ACTIVE_WORK,
            "docs/roadmap/program.md": plan,
        }

    def test_lifecycle_header_rejects_missing_unknown_and_dangling_fields(self) -> None:
        cases = {
            "missing owner": (
                "# Program\n\n"
                "- **Status:** active\n"
                "- **Completion / archival evidence:** pending\n",
                "project.roadmap_lifecycle_header_missing",
            ),
            "unknown status": (
                "# Program\n\n"
                "- **Status:** maybe\n"
                f"- **Owning queue item:** {self.OWNER}\n"
                "- **Completion / archival evidence:** pending\n",
                "project.roadmap_lifecycle_status_invalid",
            ),
            "dangling owner": (
                "# Program\n\n"
                "- **Status:** active\n"
                "- **Owning queue item:** [gone](active-work.md#gone)\n"
                "- **Completion / archival evidence:** pending\n",
                "project.roadmap_lifecycle_owner_dangling",
            ),
        }
        for label, (plan, code) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(DocumentationError) as raised:
                    validate_roadmap_lifecycle(self._documents(plan))
                self.assertEqual(raised.exception.code, code)

    def test_terminal_roadmap_requires_linked_evidence_and_history_location(
        self,
    ) -> None:
        completed_without_evidence = (
            "# Program\n\n"
            "- **Status:** completed\n"
            f"- **Owning queue item:** {self.OWNER}\n"
            "- **Completion / archival evidence:** complete\n"
        )
        with self.assertRaises(DocumentationError) as raised:
            validate_roadmap_lifecycle(self._documents(completed_without_evidence))
        self.assertEqual(
            raised.exception.code,
            "project.roadmap_lifecycle_evidence_missing",
        )

        historical_in_active_area = (
            "# Program\n\n"
            "- **Status:** historical\n"
            f"- **Owning queue item:** {self.OWNER}\n"
            "- **Completion / archival evidence:** "
            "[release](../../CHANGELOG.md)\n"
        )
        with self.assertRaises(DocumentationError) as raised:
            validate_roadmap_lifecycle(self._documents(historical_in_active_area))
        self.assertEqual(
            raised.exception.code,
            "project.roadmap_lifecycle_location_invalid",
        )

        validate_roadmap_lifecycle(
            {
                "docs/roadmap/active-work.md": self.ACTIVE_WORK,
                "docs/history/roadmap/program.md": (
                    "# Program\n\n"
                    "- **Status:** historical\n"
                    "- **Owning queue item:** "
                    "[PROGRAM-001](../../roadmap/active-work.md#program-001-deliver-the-program)\n"
                    "- **Completion / archival evidence:** "
                    "[release](../../../CHANGELOG.md)\n"
                ),
            }
        )

    def test_initialized_queue_accepts_owned_partial_plan(self) -> None:
        initialized_queue = Path(
            "src/literate_ai/project_template/docs/roadmap/active-work.md"
        ).read_text(encoding="utf-8")
        plan = (
            "# Component program\n\n"
            "- **Status:** partial\n"
            "- **Owning queue item:** "
            "[ONBOARD-001](active-work.md#onboard-001-replace-this-starter-item-with-the-first-project-outcome)\n"
            "- **Completion / archival evidence:** pending while the queue item "
            "is open\n"
        )

        validate_roadmap_lifecycle(
            {
                "docs/roadmap/active-work.md": initialized_queue,
                "docs/roadmap/component-program.md": plan,
            }
        )

    def test_documentation_graph_applies_lifecycle_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = {
                "docs/README.md": ("# Guide\n\n[Work](roadmap/active-work.md)\n"),
                "docs/roadmap/active-work.md": (
                    self.ACTIVE_WORK + "\n[Program](program.md)\n"
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
