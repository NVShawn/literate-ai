"""Explicit forward-authoring selection cannot silently replace inheritance."""

import tempfile
import unittest
import hashlib
import json
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli.errors import CliFailure
from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)
from literate_ai.cli.source_to_specification import (
    _PROMOTION_TEMPLATE_AUTHORING,
    _select_promotion_authoring,
)


class PromotionAuthoringSelectionTests(unittest.TestCase):
    def old_parent(self, root):
        fixture = json.loads(
            (Path(__file__).parents[1] / "fixtures/promotion_authoring_875.json")
            .read_bytes()
        )
        self.assertEqual(
            fixture["revision"], "87506c804210c46835723e11d1ca1c6cae0f496c"
        )
        for row in fixture["files"]:
            content = row["utf8"].encode("utf-8")
            self.assertEqual(hashlib.sha256(content).hexdigest(), row["sha256"])
            path = root / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return AgentSkillCatalog.discover(root, catalog_roots=(root / "skills",))

    def test_actual_old_parent_consumer_dependency_survives_installed_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inherited = self.old_parent(root)
            consumer = inherited.by_name("cpp17-portable-json-application")
            original = consumer.content
            self.assertEqual(consumer.dependencies[0].version, "1.8.4")
            selected = _select_promotion_authoring(root, "installed")
            try:
                current = AgentSkillCatalog.discover(
                    root, catalog_roots=(root / "skills",)
                )
            except AgentSkillCatalogError as exc:
                self.fail(f"OLD_PARENT_CONSUMER_REFERENCE_STALE: {exc}")
            changed = current.by_name(consumer.name)
            implementation = current.by_name("portable-application-implementation")
            self.assertEqual(changed.dependencies[0].version, implementation.version)
            self.assertEqual(changed.dependencies[0].identity, implementation.identity)
            # Only reference-bearing frontmatter may migrate, never instructions.
            body = lambda content: content.split(b"\n---\n", 1)[1]
            self.assertEqual(body(changed.content), body(original))
            self.assertEqual(
                selected[str(changed.logical_manifest)],
                hashlib.sha256(changed.content).hexdigest(),
            )
            for relative in _PROMOTION_TEMPLATE_AUTHORING:
                self.assertEqual(
                    (root / relative).read_bytes(),
                    files("literate_ai.project_template")
                    .joinpath(relative)
                    .read_bytes(),
                )

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
