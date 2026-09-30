"""Application-level tooling for readable layered specification corpora."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.specifications import (
    LiterateMarkdownProvider,
    format_literate_markdown_document,
)
from literate_ai.application import (
    FormattedSpecificationCorpus,
    SpecificationCorpusError,
    SpecificationCorpusNode,
    SpecificationCorpusReport,
    SpecificationCorpusService,
)


def _document(
    name: str,
    summary: str,
    kind: str,
    *,
    extra: str = "",
    newline: str = "\n",
) -> bytes:
    text = f"""---
kind: {kind}
summary: {summary}
{extra}name: {name}
---
# {name}

Observable behavior for {name}.
"""
    return text.replace("\n", newline).encode()


class SpecificationCorpusServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        files = {
            "spec/spec.md": _document("Application", "Root behavior", "app"),
            "spec/contract.md": _document("Contract", "Shared protocol", "contract"),
            "spec/feature/spec.md": _document(
                "Feature", "Bounded feature", "component"
            ),
            "spec/feature/backend.md": _document(
                "Backend",
                "Backend behavior",
                "part",
                extra=("references:\n  - example.contract\nparent: example.feature\n"),
                newline="\r\n",
            ),
            "spec/config.json": b'{"limit":3}\n',
        }
        for relative, content in files.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.paths = (
            "spec/spec.md",
            "spec/feature/backend.md",
            "spec/config.json",
            "spec/contract.md",
            "spec/feature/spec.md",
        )
        self.service = SpecificationCorpusService(
            LiterateMarkdownProvider(), format_literate_markdown_document
        )

    def test_validate_derives_stable_graph_and_effective_context(self) -> None:
        report = self.service.validate(self.root, self.paths, id_prefix="example")

        self.assertIsInstance(report, SpecificationCorpusReport)
        self.assertEqual(report.root_id, "example")
        self.assertEqual(
            tuple(node.node_id for node in report.nodes),
            (
                "example",
                "example.contract",
                "example.feature",
                "example.feature.backend",
            ),
        )
        backend = report.node("example.feature.backend")
        self.assertEqual(backend.parent, "example.feature")
        self.assertEqual(backend.references, ("example.contract",))
        self.assertEqual(
            backend.effective_documents,
            (
                "example",
                "example.feature",
                "example.contract",
                "example.feature.backend",
            ),
        )
        self.assertEqual(report.canonical_bytes, report.canonical_bytes)
        self.assertEqual(
            report.to_dict()["schema"], "literate-ai/specification-corpus-report@1"
        )

    def test_explain_selects_a_node_or_returns_the_whole_corpus(self) -> None:
        complete = self.service.explain(self.root, self.paths, id_prefix="example")
        selected = self.service.explain(
            self.root,
            self.paths,
            id_prefix="example",
            node_id="example.feature.backend",
        )

        self.assertIsInstance(complete, SpecificationCorpusReport)
        self.assertIsInstance(selected, SpecificationCorpusNode)
        self.assertEqual(selected.node_id, "example.feature.backend")
        with self.assertRaisesRegex(SpecificationCorpusError, "no specification node"):
            self.service.explain(
                self.root,
                self.paths,
                id_prefix="example",
                node_id="example.absent",
            )

    def test_validate_and_explain_have_no_filesystem_write_surface(self) -> None:
        before = self._snapshot()
        with (
            patch.object(Path, "write_bytes", side_effect=AssertionError("write")),
            patch.object(Path, "write_text", side_effect=AssertionError("write")),
            patch.object(Path, "unlink", side_effect=AssertionError("unlink")),
            patch.object(Path, "replace", side_effect=AssertionError("replace")),
        ):
            self.service.validate(self.root, self.paths, id_prefix="example")
            self.service.explain(
                self.root,
                self.paths,
                id_prefix="example",
                node_id="example.contract",
            )
        self.assertEqual(self._snapshot(), before)

    def test_format_is_in_memory_canonical_idempotent_and_keeps_json_exact(
        self,
    ) -> None:
        before = self._snapshot()
        formatted = self.service.format(self.root, self.paths, id_prefix="example")

        self.assertIsInstance(formatted, FormattedSpecificationCorpus)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(
            tuple(item.path for item in formatted.artifacts),
            (
                "spec/spec.md",
                "spec/config.json",
                "spec/contract.md",
                "spec/feature/spec.md",
                "spec/feature/backend.md",
            ),
        )
        by_path = {item.path: item for item in formatted.artifacts}
        self.assertEqual(by_path["spec/config.json"].content, b'{"limit":3}\n')
        self.assertFalse(by_path["spec/config.json"].changed)
        backend = by_path["spec/feature/backend.md"].content
        self.assertNotIn(b"\r", backend)
        self.assertTrue(
            backend.startswith(
                b'---\nname: "Backend"\nsummary: "Backend behavior"\nkind: part\n'
            )
        )
        self.assertIn(b"parent: example.feature\nreferences:\n", backend)

        # The adapter formatter is canonical: previewing already formatted bytes is
        # byte-identical, without needing to write them into the corpus.
        for artifact in formatted.artifacts:
            if artifact.path.endswith(".md"):
                self.assertEqual(
                    format_literate_markdown_document(artifact.path, artifact.content),
                    artifact.content,
                )

    def test_validation_errors_preserve_stable_provider_code_and_do_not_mutate(
        self,
    ) -> None:
        broken = self.root / "spec/contract.md"
        broken.write_text("---\nname: Broken\n---\n", encoding="utf-8")
        before = self._snapshot()

        with self.assertRaises(SpecificationCorpusError) as caught:
            self.service.validate(self.root, self.paths, id_prefix="example")

        self.assertEqual(
            caught.exception.code, "literate_markdown.frontmatter_required"
        )
        self.assertEqual(self._snapshot(), before)

    def test_manifest_tail_order_does_not_change_report(self) -> None:
        forward = self.service.validate(self.root, self.paths, id_prefix="example")
        reordered = self.service.validate(
            self.root,
            (self.paths[0], *reversed(self.paths[1:])),
            id_prefix="example",
        )
        self.assertEqual(forward, reordered)

    def _snapshot(self) -> tuple[tuple[str, bytes, int], ...]:
        return tuple(
            (
                path.relative_to(self.root).as_posix(),
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        )


if __name__ == "__main__":
    unittest.main()
