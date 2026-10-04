"""Source previews cannot escape or disagree with their existing graph bindings."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_source_excerpts as excerpts
from literate_ai.adapters.html_surfaces import bind_authority_graph
from literate_ai.authority_graph import AuthorityGraph, AuthorityGraphNode
from literate_ai.contracts.html_observability import HtmlRenderRefusal, HtmlView


def source_for(paths: dict[str, bytes]):
    graph = AuthorityGraph.create(
        "example",
        tuple(
            AuthorityGraphNode(
                str(index),
                "component",
                path,
                "example",
                "local",
                properties=(
                    ("path", path),
                    ("identity", "sha256:" + hashlib.sha256(content).hexdigest()),
                ),
            )
            for index, (path, content) in enumerate(paths.items())
        ),
        (),
    )
    return bind_authority_graph(graph, HtmlView("dag", "1.0.0", "project", "example"))


class HtmlSourceExcerptTests(unittest.TestCase):
    def test_links_and_linked_parent_directories_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "real").mkdir()
            (root / "real/one.md").write_bytes(b"x")
            try:
                (root / "one.md").symlink_to(root / "real/one.md")
                (root / "linked").symlink_to(root / "real", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("host cannot create symbolic links")
            for path in ("one.md", "linked/one.md"):
                with (
                    self.subTest(path=path),
                    mock.patch.object(excerpts.os, "read") as read,
                ):
                    self.assertIsInstance(
                        excerpts.load_source_excerpts(root, source_for({path: b"x"})),
                        HtmlRenderRefusal,
                    )
                    read.assert_not_called()
