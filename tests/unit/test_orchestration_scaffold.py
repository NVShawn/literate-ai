"""Real canonical validation of planned root bytes, not transaction acceptance."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import orchestration_scaffold as scaffold
from literate_ai.adapters.orchestration_scaffold import prepare_orchestration_scaffold
from literate_ai.adapters.project_validation import FilesystemProjectValidationAdapter
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.projects import discover_project, project_skill_catalog
from tests.support.fixtures_test_repository_orchestration_contracts import authority


class OrchestrationScaffoldTests(unittest.TestCase):
    def prepare(self, **overrides):
        return prepare_orchestration_scaffold(
            overrides.pop("authority", authority()),
            project_id=overrides.pop("project_id", "super"),
            version=overrides.pop("version", "1.0.0"),
            **overrides,
        )

    def materialize_fixture(self, root, prepared):
        for relative, content in prepared.files:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def test_planning_reads_resources_without_writing_or_starting_processes(self):
        with (
            patch("subprocess.run", side_effect=AssertionError("process")),
            patch.object(Path, "write_bytes", side_effect=AssertionError("write")),
            patch.object(Path, "write_text", side_effect=AssertionError("write")),
            patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")),
        ):
            first = self.prepare()
            second = self.prepare()
        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)
        with self.assertRaises(FrozenInstanceError):
            first.files = ()

    def test_exact_owned_paths_do_not_seed_child_catalogs_or_execution_claims(self):
        result = self.prepare()
        self.assertEqual(
            {path for path, _content in result.files},
            {
                "SKILL.md",
                "PROJECT.md",
                "literate.project.json",
                ".literate/repository-parent.json",
                ".literate/repository-lineage.json",
                ".literate/orchestration/docs/overview.md",
                ".literate/orchestration/skills/agent/SKILL.md",
            },
        )
        self.assertEqual(result.definition.component_roots, ())
        self.assertEqual(result.definition.flavor_roots, ())
        self.assertIsNone(result.definition.lifecycle_driver)
        self.assertIsNone(result.definition.test_receipt)
        self.assertEqual(result.definition.source_intelligence.provider_id, "none")
        self.assertEqual(result.definition.repository_orchestration, authority())

    def test_real_project_validator_accepts_exact_scaffold_in_two_locations(self):
        prepared = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            results = []
            for name in ("first", "other"):
                root = Path(directory).resolve() / name
                self.materialize_fixture(root, prepared)
                child = root / "services/app"
                child.mkdir(parents=True)
                (child / "literate.project.json").write_bytes(b"not root authority")
                result = FilesystemProjectValidationAdapter().validate(
                    root,
                    require_authority_review=True,
                    synchronize_source_intelligence=False,
                )
                self.assertEqual(result["authority_review"]["state"], "current")
                self.assertEqual(result["components"], [])
                selection, lineage = FilesystemRepositoryLineageStore(root).load()
                self.assertEqual(lineage.nodes, ())
                self.assertEqual(lineage.selection, selection)
                loaded = discover_project(root)
                self.assertEqual(loaded.definition, prepared.definition)
                self.assertEqual(
                    {skill.name for skill in project_skill_catalog(loaded).skills},
                    {"literate-ai", "repository-orchestration-agent"},
                )
                results.append(result["authority_review"])
            self.assertEqual(results[0], results[1])

    def test_project_identity_version_pin_and_any_output_byte_bind_scaffold(self):
        baseline = self.prepare()
        changed = replace(
            authority(),
            repositories=(
                replace(authority().repositories[0], commit="e" * 40),
                authority().repositories[1],
            ),
        )
        for candidate in (
            self.prepare(project_id="other"),
            self.prepare(version="1.1.0"),
            self.prepare(authority=changed),
        ):
            self.assertNotEqual(baseline.identity, candidate.identity)
        for index, (path, content) in enumerate(baseline.files):
            with self.subTest(path=path):
                files = list(baseline.files)
                files[index] = (path, content + b"\n")
                self.assertNotEqual(
                    baseline.identity, replace(baseline, files=tuple(files)).identity
                )

    def test_invalid_identity_version_and_untyped_authority_refuse(self):
        for overrides in (
            {"project_id": "../root"},
            {"project_id": ""},
            {"project_id": "root\nname"},
            {"project_id": "a" * 65},
            {"project_id": True},
            {"version": "main"},
            {"authority": {}},
        ):
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((ValueError, TypeError)),
            ):
                self.prepare(**overrides)

    def test_packaged_templates_have_platform_independent_newlines(self):
        baseline = self.prepare()
        with tempfile.TemporaryDirectory() as directory:
            resource_root = Path(directory)
            target = resource_root / "orchestration"
            target.mkdir()
            for source in (
                scaffold.files("literate_ai.project_template")
                .joinpath("orchestration")
                .iterdir()
            ):
                if source.name.endswith(".md"):
                    (target / source.name).write_bytes(
                        source.read_bytes()
                        .replace(b"\r\n", b"\n")
                        .replace(b"\n", b"\r\n")
                    )
            with patch.object(scaffold, "files", return_value=resource_root):
                self.assertEqual(self.prepare(), baseline)

    def test_missing_or_duplicate_review_placeholder_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            resource_root = Path(directory)
            target = resource_root / "orchestration"
            target.mkdir()
            for source in (
                scaffold.files("literate_ai.project_template")
                .joinpath("orchestration")
                .iterdir()
            ):
                if source.name.endswith(".md"):
                    (target / source.name).write_bytes(source.read_bytes())
            guide = target / "overview.md"
            original = guide.read_text()
            for count in (0, 2):
                with self.subTest(count=count):
                    guide.write_text(
                        original.replace(
                            scaffold.AUTHORITY_REVIEW_PLACEHOLDER,
                            scaffold.AUTHORITY_REVIEW_PLACEHOLDER * count,
                        )
                    )
                    with patch.object(scaffold, "files", return_value=resource_root):
                        with self.assertRaisesRegex(ValueError, "exactly one"):
                            self.prepare()
