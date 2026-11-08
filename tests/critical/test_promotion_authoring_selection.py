"""Explicit forward-authoring selection cannot silently replace inheritance."""

import hashlib
import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.source_to_specification import (
    _PROMOTION_TEMPLATE_AUTHORING,
    _select_promotion_authoring,
)
from literate_ai.contracts.authoring_markdown import parse_authoring_markdown


class PromotionAuthoringSelectionTests(unittest.TestCase):
    def old_parent(self, root):
        fixture = json.loads(
            (
                Path(__file__).parents[1] / "fixtures/promotion_authoring_875.json"
            ).read_bytes()
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
            self.assertEqual(consumer.dependencies[0].version, "1.8.4")
            consumers = [
                skill
                for skill in inherited.skills
                if any(
                    d.skill_id == "portable-application-implementation"
                    for d in skill.dependencies
                )
            ]
            self.assertEqual(len(consumers), 9)
            selected = _select_promotion_authoring(root, "installed")
            try:
                current = AgentSkillCatalog.discover(
                    root, catalog_roots=(root / "skills",)
                )
            except AgentSkillCatalogError as exc:
                self.fail(f"OLD_PARENT_CONSUMER_REFERENCE_STALE: {exc}")
            implementation = current.by_name("portable-application-implementation")
            for original in consumers:
                changed = current.by_name(original.name)
                reference = next(
                    d for d in changed.dependencies if d.skill_id == implementation.name
                )
                self.assertEqual(reference.version, implementation.version)
                self.assertEqual(reference.identity, implementation.identity)
                # Only reference-bearing metadata may migrate, never instructions.
                self.assertEqual(
                    changed.content.split(b"\n---\n", 1)[1],
                    original.content.split(b"\n---\n", 1)[1],
                )
                self.assertEqual(
                    selected[str(changed.logical_manifest)],
                    hashlib.sha256(changed.content).hexdigest(),
                )
                old_metadata, _ = parse_authoring_markdown(
                    original.content, source=str(original.logical_manifest)
                )
                new_metadata, _ = parse_authoring_markdown(
                    changed.content, source=str(changed.logical_manifest)
                )
                for metadata in (old_metadata, new_metadata):
                    for item in metadata["dependencies"]:
                        if item["skill_id"] == implementation.name:
                            item.pop("version")
                            item["identity"].pop("digest")
                self.assertEqual(new_metadata, old_metadata)
            for relative in _PROMOTION_TEMPLATE_AUTHORING:
                self.assertEqual(
                    (root / relative).read_bytes(),
                    files("literate_ai.project_template")
                    .joinpath(relative)
                    .read_bytes(),
                )

    def staged(self, root):
        self.old_parent(root)

    def test_default_preserves_inheritance_and_explicit_selection_uses_templates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.staged(root)
            originals = {
                n: (root / n).read_bytes() for n in _PROMOTION_TEMPLATE_AUTHORING
            }
            inherited = _select_promotion_authoring(root, "inherited")
            self.assertTrue(
                all(
                    (root / n).read_bytes() == originals[n]
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
                first_before = (root / _PROMOTION_TEMPLATE_AUTHORING[0]).read_bytes()
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
                    first_before,
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

    def test_unrelated_stale_dependency_refuses_without_partial_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            catalog = self.old_parent(root)
            planning = catalog.by_name("portable-specification-planning")
            path = root / str(planning.logical_manifest)
            path.write_bytes(planning.content.replace(b'"1.1.0"', b'"9.9.9"', 1))
            before = {
                str(p.relative_to(root)): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            with self.assertRaises(CliFailure) as caught:
                _select_promotion_authoring(root, "installed")
            self.assertEqual(
                caught.exception.code, "agent_skill.dependency_version_stale"
            )
            self.assertEqual(
                before,
                {
                    str(p.relative_to(root)): p.read_bytes()
                    for p in root.rglob("*")
                    if p.is_file()
                },
            )

    def test_unsupported_reference_document_refuses_before_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            catalog = self.old_parent(root)
            consumer = catalog.by_name("cpp17-portable-json-application")
            path = root / str(consumer.logical_manifest)
            metadata, _ = parse_authoring_markdown(consumer.content, source=str(path))
            path.unlink()
            path.with_name("skill.json").write_text(json.dumps(metadata))
            AgentSkillCatalog.discover(root, catalog_roots=(root / "skills",))
            implementation = root / _PROMOTION_TEMPLATE_AUTHORING[0]
            original = implementation.read_bytes()
            with self.assertRaises(CliFailure) as caught:
                _select_promotion_authoring(root, "installed")
            self.assertEqual(
                caught.exception.code, "authoring_markdown.frontmatter_missing"
            )
            self.assertEqual(implementation.read_bytes(), original)
