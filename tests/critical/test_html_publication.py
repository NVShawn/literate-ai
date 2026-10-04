"""Output publication refuses collisions and preserves concurrently replaced files."""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_publication as publication


class HtmlPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / "graph.html"

    def publish(self, previous=None, *, revalidate=lambda: None):
        publication.publish_html(
            self.target, b"completed fixture", previous, revalidate=revalidate
        )

    def assert_no_temporary(self):
        self.assertFalse(list(self.root.rglob(".litai-html-*")))

    def writer_close_effect(self, effect):
        fdopen = os.fdopen

        @contextmanager
        def closing(*args, **kwargs):
            with fdopen(*args, **kwargs) as stream:
                yield stream
            effect(next(self.root.glob(".litai-html-*")))

        return mock.patch.object(publication.os, "fdopen", side_effect=closing)

    def test_replaced_file_on_writer_close_is_preserved(self):
        def replace(path):
            path.rename(self.root / "retained-original")
            path.write_bytes(b"completed fixture")

        with self.writer_close_effect(replace):
            with self.assertRaisesRegex(ValueError, "changed after writing"):
                self.publish()
        self.assertFalse(self.target.exists())
        temporary = next(self.root.glob(".litai-html-*"))
        self.assertEqual(temporary.read_bytes(), b"completed fixture")

    def test_existing_foreign_destination_refuses_no_clobber_publish(self):
        self.target.write_bytes(b"foreign")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.target.read_bytes(), b"foreign")
        self.assert_no_temporary()
