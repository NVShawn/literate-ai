"""Retained source review is exact input custody, never acceptance."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.retained_source import (
    RetainedSourceError,
    RetainedSourceInput,
)
from literate_ai.contracts import canonical_identity


class RetainedSourceTests(unittest.TestCase):
    def test_error_preserves_structured_cli_message(self):
        error = RetainedSourceError("retained_source.changed", "Source changed")
        self.assertEqual(error.code, "retained_source.changed")
        self.assertEqual(error.message, "Source changed")
        self.assertEqual(str(error), "Source changed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "main.py").write_bytes(b"print('retained')\r\n")

    def capture(self):
        return RetainedSourceInput.capture(
            self.root,
            component_lock_identity=canonical_identity({"lock": 1}),
            project_authority_identity=canonical_identity({"project": 1}),
            target="host",
        )

    def test_exact_review_and_bytes(self):
        value = self.capture()
        self.assertEqual(value.files, (("source/main.py", b"print('retained')\r\n"),))
        for wrong in (None, "", value.tree_identity.uri):
            with self.assertRaises(RetainedSourceError):
                value.require_authorization(wrong)
        value.require_authorization(value.identity.uri)

    def test_authorization_binds_target_lock_and_project(self):
        value = self.capture()
        for changed in (
            replace(value, target="other"),
            replace(value, component_lock_identity=canonical_identity({"lock": 2})),
            replace(
                value, project_authority_identity=canonical_identity({"project": 2})
            ),
        ):
            with self.assertRaises(RetainedSourceError):
                changed.require_authorization(value.identity.uri)

    def test_changed_input_refused(self):
        value = self.capture()
        (self.root / "main.py").write_bytes(b"changed")
        with self.assertRaises(RetainedSourceError):
            value.require_authorization(value.identity.uri)

    def test_added_input_refused(self):
        value = self.capture()
        (self.root / "extra.py").write_bytes(b"extra")
        with self.assertRaises(RetainedSourceError):
            value.require_unchanged()

    def test_binary_input_refused(self):
        (self.root / "bytecode.pyc").write_bytes(b"\xff")
        with self.assertRaises(RetainedSourceError):
            self.capture()

    def test_unreadable_subtree_is_not_silently_omitted(self):
        def unreadable(_root, *, followlinks, onerror):
            onerror(PermissionError("unreadable subtree"))
            return iter(())

        with mock.patch(
            "literate_ai.adapters.retained_source.os.walk", side_effect=unreadable
        ):
            with self.assertRaisesRegex(RetainedSourceError, "completely read"):
                self.capture()

    def test_nonportable_path_refused(self):
        try:
            (self.root / "aux.py").write_bytes(b"pass")
        except OSError:
            self.skipTest("host already rejects the nonportable filename")
        with self.assertRaises(RetainedSourceError):
            self.capture()

    def test_link_input_refused(self):
        try:
            (self.root / "linked.py").symlink_to(self.root / "main.py")
        except OSError:
            self.skipTest("host cannot create test symlinks")
        with self.assertRaises(RetainedSourceError):
            self.capture()
