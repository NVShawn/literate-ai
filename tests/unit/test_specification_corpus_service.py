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
