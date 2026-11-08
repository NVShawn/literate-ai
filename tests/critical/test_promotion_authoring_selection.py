"""Explicit forward-authoring selection cannot silently replace inheritance."""

import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli.errors import CliFailure
from literate_ai.cli.source_to_specification import (
    _PROMOTION_TEMPLATE_AUTHORING,
    _select_promotion_authoring,
)


class PromotionAuthoringSelectionTests(unittest.TestCase):
    def staged(self, root):
        for relative in _PROMOTION_TEMPLATE_AUTHORING:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"inherited authority\n")

    def test_default_preserves_inheritance_and_explicit_selection_uses_templates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.staged(root)
            inherited = _select_promotion_authoring(root, "inherited")
            self.assertTrue(
                all(
                    (root / n).read_bytes() == b"inherited authority\n"
                    for n in _PROMOTION_TEMPLATE_AUTHORING
                )
            )
            selected = _select_promotion_authoring(root, "installed")
            self.assertNotEqual(selected, inherited)
            for relative in _PROMOTION_TEMPLATE_AUTHORING:
                self.assertEqual(
                    (root / relative).read_bytes(),
                    files("literate_ai.project_template")
                    .joinpath(relative)
                    .read_bytes(),
                )
            with self.assertRaises(CliFailure):
                _select_promotion_authoring(root, "unreviewed")

    def test_missing_nonregular_and_drift_refuse(self):
        for kind in ("missing", "directory", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.staged(root)
                path = root / _PROMOTION_TEMPLATE_AUTHORING[-1]
                path.unlink()
                if kind == "directory":
                    path.mkdir()
                elif kind == "symlink":
                    path.symlink_to(root / _PROMOTION_TEMPLATE_AUTHORING[0])
                with self.assertRaises(CliFailure):
                    _select_promotion_authoring(root, "installed")
                self.assertEqual(
                    (root / _PROMOTION_TEMPLATE_AUTHORING[0]).read_bytes(),
                    b"inherited authority\n",
                )
                package = root / "package"
                self.staged(package)
                resource = package / _PROMOTION_TEMPLATE_AUTHORING[-1]
                resource.unlink()
                if kind == "directory":
                    resource.mkdir()
                elif kind == "symlink":
                    resource.symlink_to(package / _PROMOTION_TEMPLATE_AUTHORING[0])
                with patch("importlib.resources.files", return_value=package):
                    with self.assertRaises(CliFailure):
                        _select_promotion_authoring(root, "installed")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.staged(root)
            template = root / "package"
            self.staged(template)
            reads = {}
            original = Path.read_bytes

            def changed(path):
                body = original(path)
                if path.is_relative_to(template):
                    reads[path] = reads.get(path, 0) + 1
                    if reads[path] > 1:
                        return body + b"changed"
                return body

            with (
                patch("importlib.resources.files", return_value=template),
                patch.object(Path, "read_bytes", changed),
            ):
                with self.assertRaises(CliFailure) as caught:
                    _select_promotion_authoring(root, "installed")
                self.assertEqual(
                    caught.exception.code, "promotion.authoring_template_changed"
                )
