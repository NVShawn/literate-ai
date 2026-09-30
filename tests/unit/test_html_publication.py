"""Output publication refuses collisions and preserves concurrently replaced files."""

import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
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

    def test_write_timestamp_finalized_on_close_is_accepted(self):
        def finalize_timestamp(path):
            observed = path.stat()
            os.utime(
                path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 2_000_000_000)
            )

        with self.writer_close_effect(finalize_timestamp):
            self.publish()
        self.assertEqual(self.target.read_bytes(), b"completed fixture")
        self.assert_no_temporary()

    def test_distinct_path_and_descriptor_ctime_clocks_are_accepted(self):
        # CPython 3.12 Windows lstat reports birth time as ctime, whereas fstat
        # can report metadata change time. They remain stable independently.
        fstat = os.fstat

        def descriptor_metadata(descriptor):
            node = fstat(descriptor)
            if not stat.S_ISREG(node.st_mode):
                return node
            return SimpleNamespace(
                st_dev=node.st_dev,
                st_ino=node.st_ino,
                st_size=node.st_size,
                st_mtime_ns=node.st_mtime_ns,
                st_ctime_ns=node.st_ctime_ns + 2_000_000_000,
                st_mode=node.st_mode,
            )

        with mock.patch.object(
            publication.os, "fstat", side_effect=descriptor_metadata
        ):
            self.publish()
            self.assertEqual(
                publication.read_output(self.target).content, b"completed fixture"
            )
        self.assert_no_temporary()

    def test_descriptor_ctime_drift_during_read_is_rejected(self):
        self.target.write_bytes(b"fixture")
        fstat = os.fstat
        observations = 0

        def descriptor_metadata(descriptor):
            nonlocal observations
            node = fstat(descriptor)
            if not stat.S_ISREG(node.st_mode):
                return node
            observations += 1
            return SimpleNamespace(
                st_dev=node.st_dev,
                st_ino=node.st_ino,
                st_size=node.st_size,
                st_mtime_ns=node.st_mtime_ns,
                st_ctime_ns=node.st_ctime_ns + observations * 2_000_000_000,
                st_mode=node.st_mode,
            )

        with (
            mock.patch.object(publication.os, "fstat", side_effect=descriptor_metadata),
            self.assertRaisesRegex(ValueError, "changed while reading"),
        ):
            publication.read_output(self.target)
        self.assertEqual(self.target.read_bytes(), b"fixture")

    def test_path_ctime_drift_during_read_is_rejected(self):
        self.target.write_bytes(b"fixture")
        lstat = Path.lstat
        observations = 0

        def named_metadata(path, *args, **kwargs):
            nonlocal observations
            node = lstat(path, *args, **kwargs)
            if path != self.target:
                return node
            observations += 1
            return SimpleNamespace(
                st_dev=node.st_dev,
                st_ino=node.st_ino,
                st_size=node.st_size,
                st_mtime_ns=node.st_mtime_ns,
                st_ctime_ns=node.st_ctime_ns + (observations > 2) * 2_000_000_000,
                st_mode=node.st_mode,
                st_file_attributes=getattr(node, "st_file_attributes", 0),
            )

        with (
            mock.patch.object(Path, "lstat", new=named_metadata),
            self.assertRaisesRegex(ValueError, "changed while reading"),
        ):
            publication.read_output(self.target)
        self.assertEqual(self.target.read_bytes(), b"fixture")

    def test_changed_bytes_on_writer_close_are_refused(self):
        with self.writer_close_effect(
            lambda path: path.write_bytes(b"changed fixture")
        ):
            with self.assertRaisesRegex(ValueError, "changed after writing"):
                self.publish()
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()

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

    def test_publish_complete_new_bytes_and_replace_owned_snapshot(self):
        self.publish()
        previous = publication.read_output(self.target)
        self.assertEqual(previous.content, b"completed fixture")
        publication.publish_html(
            self.target, b"new completed bytes", previous, revalidate=lambda: None
        )
        self.assertEqual(self.target.read_bytes(), b"new completed bytes")
        self.assert_no_temporary()

    def test_unchanged_bytes_still_revalidate_without_replacing_file(self):
        self.publish()
        previous = publication.read_output(self.target)
        revalidate = mock.Mock()
        self.publish(previous, revalidate=revalidate)
        revalidate.assert_called_once_with()
        self.assertEqual(publication.read_output(self.target), previous)

    def test_path_refuses_metadata_and_parent_traversal(self):
        for path in ("../graph.html", ".git/report.html", ".litai-locks/report.html"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                publication.html_output_path(self.root, path)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_nested_parent_is_created_only_for_publication(self):
        self.target = publication.html_output_path(self.root, "new/graph.html")
        self.assertFalse(self.target.parent.exists())
        self.publish()
        self.assertEqual(self.target.read_bytes(), b"completed fixture")
        self.assert_no_temporary()

    def test_existing_foreign_destination_refuses_no_clobber_publish(self):
        self.target.write_bytes(b"foreign")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.target.read_bytes(), b"foreign")
        self.assert_no_temporary()

    def test_destination_appearing_at_atomic_link_is_preserved(self):
        link = os.link

        def concurrent(source, target, **kwargs):
            self.target.write_bytes(b"concurrently published foreign file")
            return link(source, target, **kwargs)

        with mock.patch.object(publication.os, "link", side_effect=concurrent):
            with self.assertRaises(FileExistsError):
                self.publish()
        self.assertEqual(
            self.target.read_bytes(), b"concurrently published foreign file"
        )
        self.assert_no_temporary()

    def test_changed_destination_at_final_revalidation_is_preserved(self):
        self.target.write_bytes(b"previous")
        previous = publication.read_output(self.target)
        with self.assertRaises(ValueError):
            self.publish(
                previous, revalidate=lambda: self.target.write_bytes(b"foreign change")
            )
        self.assertEqual(self.target.read_bytes(), b"foreign change")
        self.assert_no_temporary()

    def test_failed_revalidation_preserves_previous_output_and_removes_temporary(self):
        self.target.write_bytes(b"previous")
        previous = publication.read_output(self.target)
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.publish(
                previous, revalidate=mock.Mock(side_effect=ValueError("source changed"))
            )
        self.assertEqual(self.target.read_bytes(), b"previous")
        self.assert_no_temporary()

    def test_replace_failure_preserves_previous_output(self):
        self.target.write_bytes(b"previous")
        previous = publication.read_output(self.target)
        with mock.patch.object(
            publication.os, "replace", side_effect=OSError("fixture")
        ):
            with self.assertRaises(OSError):
                self.publish(previous)
        self.assertEqual(self.target.read_bytes(), b"previous")
        self.assert_no_temporary()

    def test_file_flush_failure_leaves_no_partial_destination_or_temporary(self):
        with mock.patch.object(publication.os, "fsync", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.publish()
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()

    def test_read_limit_refuses_without_opening_target(self):
        self.target.write_bytes(b"oversized fixture")
        with (
            mock.patch.object(publication, "MAXIMUM_HTML_BYTES", 1),
            mock.patch.object(publication.os, "open", wraps=os.open) as opened,
        ):
            with self.assertRaises(ValueError):
                publication.read_output(self.target)
        self.assertFalse(
            any(call.args[0] == self.target for call in opened.call_args_list)
        )

    def test_read_failure_closes_descriptor(self):
        self.target.write_bytes(b"fixture")
        descriptors = []

        def failed(descriptor, count):
            descriptors.append(descriptor)
            raise OSError("fixture")

        with mock.patch.object(publication.os, "read", side_effect=failed):
            with self.assertRaises(OSError):
                publication.read_output(self.target)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_output_link_is_refused_and_its_target_is_preserved(self):
        foreign = self.root / "foreign.html"
        foreign.write_bytes(b"foreign")
        try:
            self.target.symlink_to(foreign)
        except OSError as exc:
            self.skipTest(f"host cannot create a symbolic link: {exc}")
        with self.assertRaises(ValueError):
            publication.html_output_path(self.root, "graph.html")
        with self.assertRaises(ValueError):
            publication.read_output(self.target)
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_replaced_temporary_file_is_preserved(self):
        retained = self.root / "owned-retained"

        def replace_temporary():
            temporary = next(self.root.glob(".litai-html-*"))
            temporary.rename(retained)
            temporary.write_bytes(b"foreign temporary-name occupant")

        with self.assertRaises(ValueError):
            self.publish(revalidate=replace_temporary)
        self.assertFalse(self.target.exists())
        temporary = next(self.root.glob(".litai-html-*"))
        self.assertEqual(temporary.read_bytes(), b"foreign temporary-name occupant")
        self.assertEqual(retained.read_bytes(), b"completed fixture")
