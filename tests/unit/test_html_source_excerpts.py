"""Source previews cannot escape or disagree with their existing graph bindings."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_source_excerpts as excerpts
from literate_ai.adapters.html_surfaces import bind_authority_graph
from literate_ai.authority_graph import AuthorityGraph, AuthorityGraphNode
from literate_ai.contracts.html_observability import HtmlRenderRefusal, HtmlView
from literate_ai.project_authority_graph import project_authority_graph

ROOT = Path(__file__).resolve().parents[2]


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
    def test_real_graph_excerpts_match_existing_file_hashes_without_graph_changes(
        self,
    ) -> None:
        graph = project_authority_graph(ROOT)
        source = bind_authority_graph(
            graph, HtmlView("dag", "1.0.0", "project", graph.project_id)
        )
        original = source.json_bytes
        observed = excerpts.load_source_excerpts(ROOT, source)
        self.assertIsInstance(observed, tuple)
        self.assertEqual(len(observed), len(graph.nodes))
        available = [item for item in observed if item.text is not None]
        self.assertGreater(len(available), 100)
        for item in available:
            content = (ROOT / item.path).read_bytes()
            self.assertEqual(
                item.source_identity.digest, hashlib.sha256(content).hexdigest()
            )
            self.assertTrue(content.decode().startswith(item.text))
        self.assertEqual(source.json_bytes, original)
        self.assertEqual(source.binding.source_identity.uri, graph.identity)

    def test_utf8_json_and_markdown_preserve_decoded_content(self) -> None:
        paths = {
            "one.md": "# Ω\r\n</script><!-- &\n".encode(),
            "two.json": b'{"ok":true}\n',
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for path, content in paths.items():
                (root / path).write_bytes(content)
            source = source_for(paths)
            observed = excerpts.load_source_excerpts(root, source)
            self.assertIsInstance(observed, tuple)
            self.assertEqual(
                [item.text for item in observed], [v.decode() for v in paths.values()]
            )
            self.assertTrue(all(not item.truncated for item in observed))
            first = observed[0].to_dict()
            first["text"] = "changed"
            self.assertNotEqual(observed[0].text, first["text"])

    def test_limits_truncate_display_only_after_verifying_whole_source(self) -> None:
        for text in ("line\n" * 100, "Ω" * 9000, ""):
            with (
                self.subTest(size=len(text)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                (root / "one.md").write_bytes(text.encode())
                source = source_for({"one.md": text.encode()})
                observed = excerpts.load_source_excerpts(root, source)
                self.assertIsInstance(observed, tuple)
                self.assertLessEqual(len(observed[0].text), excerpts.EXCERPT_CHARACTERS)
                self.assertLessEqual(
                    len(observed[0].text.splitlines()), excerpts.EXCERPT_LINES
                )
                self.assertEqual(observed[0].truncated, bool(text))
                if text:
                    (root / "one.md").write_bytes((text[:-1] + "X").encode())
                    self.assertIsInstance(
                        excerpts.load_source_excerpts(root, source), HtmlRenderRefusal
                    )

    def test_missing_local_binding_is_explicit_and_never_guessed(self) -> None:
        graph = AuthorityGraph.create(
            "example",
            (
                AuthorityGraphNode(
                    "repo",
                    "repository",
                    "example",
                    "example",
                    "https://example.invalid",
                ),
                AuthorityGraphNode(
                    "candidate",
                    "component",
                    "one",
                    "parent",
                    "remote",
                    properties=(("identity", "sha256:" + "a" * 64),),
                ),
            ),
            (),
        )
        source = bind_authority_graph(
            graph, HtmlView("dag", "1.0.0", "project", "example")
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(excerpts, "_read_bound_source") as read,
        ):
            observed = excerpts.load_source_excerpts(Path(temporary), source)
        self.assertIsInstance(observed, tuple)
        self.assertTrue(
            all(item.text is None and item.path is None for item in observed)
        )
        self.assertTrue(all(item.to_dict()["unavailable_reason"] for item in observed))
        read.assert_not_called()

    def test_unsafe_or_unsupported_paths_refuse_without_reading(self) -> None:
        for path in (
            "../outside.md",
            "/outside.md",
            "one/../../outside.md",
            "one\\two.md",
            "one.exe",
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temporary:
                with mock.patch.object(excerpts.os, "read") as read:
                    result = excerpts.load_source_excerpts(
                        Path(temporary), source_for({path: b"x"})
                    )
                self.assertIsInstance(result, HtmlRenderRefusal)
                read.assert_not_called()

    def test_missing_changed_invalid_utf8_and_oversized_sources_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = source_for({"one.md": b"expected"})
            self.assertIsInstance(
                excerpts.load_source_excerpts(root, source), HtmlRenderRefusal
            )
            for content in (b"changed", b"\xff"):
                (root / "one.md").write_bytes(content)
                self.assertIsInstance(
                    excerpts.load_source_excerpts(root, source), HtmlRenderRefusal
                )
            (root / "one.md").write_bytes(b"\xff")
            invalid = source_for({"one.md": b"\xff"})
            self.assertIsInstance(
                excerpts.load_source_excerpts(root, invalid), HtmlRenderRefusal
            )
            (root / "one.md").write_bytes(b"expected")
            with mock.patch.object(excerpts, "MAXIMUM_SOURCE_BYTES", 2):
                self.assertIsInstance(
                    excerpts.load_source_excerpts(root, source), HtmlRenderRefusal
                )

    def test_aggregate_limits_are_not_only_per_file_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {"one.md": b"one", "two.md": b"two"}
            for path, content in paths.items():
                (root / path).write_bytes(content)
            source = source_for(paths)
            for limit, value in (
                ("MAXIMUM_TOTAL_BYTES", 4),
                ("MAXIMUM_SOURCE_FILES", 1),
            ):
                with (
                    self.subTest(limit=limit),
                    mock.patch.object(excerpts, limit, value),
                ):
                    self.assertIsInstance(
                        excerpts.load_source_excerpts(root, source), HtmlRenderRefusal
                    )

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

    @unittest.skipUnless(hasattr(os, "mkfifo"), "host lacks named pipes")
    def test_fifo_is_rejected_before_opening_or_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / "one.md")
            with (
                mock.patch.object(excerpts.os, "open", wraps=os.open) as opened,
                mock.patch.object(excerpts.os, "read") as read,
            ):
                result = excerpts.load_source_excerpts(
                    root, source_for({"one.md": b"x"})
                )
                self.assertIsInstance(result, HtmlRenderRefusal)
                self.assertTrue(
                    all(
                        Path(call.args[0]).name != "one.md"
                        for call in opened.call_args_list
                    )
                )
                read.assert_not_called()

    def test_source_changed_during_observation_cannot_escape_the_hash_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "one.md"
            path.write_bytes(b"expected")
            source = source_for({"one.md": b"expected"})
            original_read = os.read
            changed = False

            def read(descriptor, size):
                nonlocal changed
                if not changed:
                    changed = True
                    path.write_bytes(b"changed!")
                return original_read(descriptor, size)

            with mock.patch.object(excerpts.os, "read", side_effect=read):
                result = excerpts.load_source_excerpts(root, source)
            self.assertIsInstance(result, HtmlRenderRefusal)
            self.assertEqual(path.read_bytes(), b"changed!")
