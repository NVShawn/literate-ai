"""End-to-end baseline, clean-merge, reviewed-resolution and rollback proofs."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.project_update_apply import apply_project_update
from literate_ai.adapters.project_updates import (
    FilesystemProjectUpdateAdapter,
    ProjectUpdateError,
)
from literate_ai.adapters.update_merge import (
    CHECKPOINT,
    UpdateBases,
    digest,
    recover_bases,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectUpdateClassification,
)
from tests.support.fixtures_test_project_update_adapter import _origin
from tests.support.root_parent_adapter import RootParentProjectInitializationAdapter

BASE = b"first\n" + b"context\n" * 10 + b"last\n"
OURS = BASE.replace(b"first", b"local first")
THEIRS = BASE.replace(b"last", b"upstream last")
MERGED = OURS.replace(b"last", b"upstream last")


class HistoricalTransportRecoveryTests(unittest.TestCase):
    REVISION = "a" * 40

    def recover(self, url, scope):
        """Run base recovery for one recorded locator; return calls and bases."""
        from types import SimpleNamespace

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        bases = UpdateBases(root)
        origin = replace(_origin("a", "1.0.0"), repository_url=url)
        ref = f"git:{url}@{self.REVISION}"
        if scope == "catalog":
            bases.sources[".gitignore"] = ref
        item = SimpleNamespace(
            path=".gitignore",
            classification=ProjectUpdateClassification.CONFLICT,
            baseline_identity=ContentIdentity.parse_uri(digest(BASE)),
        )
        calls = []

        def read(reference, paths):
            calls.append((reference, paths))
            return {"wrong": b"wrong bytes", "exact": BASE}

        recover_bases(
            root,
            (item,),
            bases,
            read,
            origin=origin if scope == "framework" else None,
        )
        self.assertEqual(origin.repository_url, url)
        if scope == "catalog":
            self.assertEqual(bases.sources[".gitignore"], ref)
        self.assertFalse((root / CHECKPOINT).exists())
        return calls, bases, item

    def test_scp_initialization_and_catalog_recover_only_exact_historical_bytes(self):
        for scope in ("framework", "catalog"):
            with self.subTest(scope=scope):
                calls, bases, item = self.recover(
                    "git@github.com:NVIDIA-dev/literate-ai.git", scope
                )
                self.assertEqual(len(calls), 1)
                reference, paths = calls[0]
                self.assertEqual(
                    reference.repository_url,
                    "ssh://git@github.com/NVIDIA-dev/literate-ai",
                )
                self.assertEqual(reference.requested_revision, self.REVISION)
                self.assertIn(".gitignore", paths)
                self.assertEqual(bases.get(item.baseline_identity), BASE)
                self.assertNotIn(digest(b"wrong bytes"), bases.blobs)

    def test_recorded_urls_keep_their_transport(self):
        for url in (
            "ssh://git@github.com/owner/private.git",
            "https://github.com/owner/public.git",
            "ssh://git@git.example.com/team/repo.git",
        ):
            for scope in ("framework", "catalog"):
                with self.subTest(url=url, scope=scope):
                    calls, bases, item = self.recover(url, scope)
                    self.assertEqual(
                        [reference.repository_url for reference, _ in calls], [url]
                    )
                    self.assertEqual(bases.get(item.baseline_identity), BASE)

    def test_unusable_historical_locator_leaves_conflict_unresolved(self):
        for url in (
            "git@git.example.com:team/repo.git",
            "deploy@github.com:owner/repo.git",
            "git@github.com:owner/repo/extra.git",
            "relative/path",
        ):
            for scope in ("framework", "catalog"):
                with self.subTest(url=url, scope=scope):
                    calls, bases, item = self.recover(url, scope)
                    self.assertEqual(calls, [])
                    self.assertIsNone(bases.get(item.baseline_identity))


class FrameworkMergeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.origin = _origin("a", "0.2.0")
        RootParentProjectInitializationAdapter(
            standard_binding_provider=lambda: None,
            initialization_origin_provider=lambda: self.origin,
        ).initialize(self.root, empty=True, source_intelligence_provider="none")
        self.target = self.root / ".gitignore"
        self.target.write_bytes(OURS)
        bases = UpdateBases(self.root)
        bases.advance("framework", ".gitignore", BASE)
        bases.write()
        self.upstream = {".gitignore": THEIRS}
        self.addCleanup(patch.stopall)
        patch(
            "literate_ai.adapters.project_updates._upstream_template",
            side_effect=lambda _: self.upstream,
        ).start()
        patch(
            "literate_ai.adapters.project_update_apply._upstream_template",
            side_effect=lambda _: self.upstream,
        ).start()
        self.adapter = FilesystemProjectUpdateAdapter(
            origin_provider=lambda: self.origin
        )

    def item(self, plan):
        return next(item for item in plan.files if item.path == ".gitignore")

    def test_two_consecutive_updates_preserve_overlay_and_advance_base(self):
        before = (self.root / CHECKPOINT).read_bytes()
        plan = self.adapter.plan(self.root)
        self.assertEqual(
            self.item(plan).classification, ProjectUpdateClassification.MERGEABLE
        )
        self.assertEqual((self.root / CHECKPOINT).read_bytes(), before)
        self.assertEqual(self.target.read_bytes(), OURS)
        if os.name != "nt":
            self.target.chmod(0o755)
        applied = apply_project_update(plan, self.root)
        self.assertEqual(applied.merged, (".gitignore",))
        self.assertEqual(self.target.read_bytes(), MERGED)
        if os.name != "nt":
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            self.item(self.adapter.plan(self.root)).classification,
            ProjectUpdateClassification.LOCAL_ONLY,
        )
        self.upstream[".gitignore"] = THEIRS.replace(b"upstream last", b"new last")
        apply_project_update(self.adapter.plan(self.root), self.root)
        self.assertEqual(
            self.target.read_bytes(), MERGED.replace(b"upstream last", b"new last")
        )
        bases = UpdateBases(self.root)
        self.assertEqual(
            bases.get(bases.identities("framework")[".gitignore"]),
            self.upstream[".gitignore"],
        )

    def test_failed_validation_restores_files_and_checkpoint_exactly(self):
        before = (self.root / CHECKPOINT).read_bytes()

        def reject(_root):
            self.assertEqual(self.target.read_bytes(), MERGED)
            raise ValueError("invalid prospective authority")

        with self.assertRaisesRegex(ValueError, "invalid prospective"):
            apply_project_update(
                self.adapter.plan(self.root), self.root, validator=reject
            )
        self.assertEqual(self.target.read_bytes(), OURS)
        self.assertEqual((self.root / CHECKPOINT).read_bytes(), before)

    def test_stale_input_refuses_before_writes(self):
        plan = self.adapter.plan(self.root)
        self.target.write_bytes(b"concurrent edit\n")
        with self.assertRaises(ProjectUpdateError) as caught:
            apply_project_update(plan, self.root)
        self.assertEqual(caught.exception.code, "project.update_local_changed")
        self.assertEqual(self.target.read_bytes(), b"concurrent edit\n")

    def test_tampered_base_fails_closed(self):
        path = self.root / CHECKPOINT
        document = json.loads(path.read_bytes())
        document["blobs"][digest(BASE)] = "d3Jvbmc="
        path.write_text(json.dumps(document))
        with self.assertRaises(ProjectUpdateError) as caught:
            self.adapter.plan(self.root)
        self.assertEqual(caught.exception.code, "project.update_base_invalid")


class CatalogMergeTests(unittest.TestCase):
    def setUp(self):
        from literate_ai.adapters.project_initialization import (
            FilesystemProjectInitializationAdapter,
        )
        from literate_ai.adapters.repository_catalogs import InheritedCatalogPlan
        from literate_ai.adapters.repository_updates import (
            FilesystemRepositoryUpdateAdapter,
        )
        from tests.support.fixtures_test_repository_updates import (
            fixture,
            flavor_item,
            origin,
        )

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        selection, _root_node, child, lineage = fixture()
        self.item = flavor_item(child, "lang-python", "lang-python")
        self.catalogs = InheritedCatalogPlan(lineage, (self.item,))
        FilesystemProjectInitializationAdapter(
            standard_binding_provider=lambda: None,
            initialization_origin_provider=origin,
            repository_lineage_resolver=lambda _: lineage,
            repository_catalog_planner=lambda _: self.catalogs,
        ).initialize(
            self.root,
            empty=True,
            source_intelligence_provider="none",
            parent_selection=selection,
            flavor_selectors=("+python", "+macos"),
        )
        self.adapter = FilesystemRepositoryUpdateAdapter(
            lineage_resolver=lambda _: self.catalogs.lineage,
            catalog_planner=lambda _: self.catalogs,
        )
        self.path = "flavors/lang-python/flavor.md"
        self.target = self.root / self.path
        self.base = self.target.read_bytes()
        # Separate edits by the entire original document. Both remain valid Markdown.
        self.ours = self.base + b"\nLocal policy\n"
        self.target.write_bytes(self.ours)

    def upstream(self, prefix):
        self.catalogs = replace(
            self.catalogs,
            items=(
                replace(
                    self.item,
                    files=tuple(
                        replace(
                            file, content=file.content.replace(b"\n", b"\n" + prefix, 1)
                        )
                        if file.destination == self.path
                        else file
                        for file in self.item.files
                    ),
                ),
            ),
        )

    def test_public_cli_merges_framework_and_catalog_in_one_transaction(self):
        from argparse import Namespace
        from types import SimpleNamespace

        from literate_ai.cli.project import update_project_from_args
        from tests.support.fixtures_test_repository_updates import origin

        self.upstream(b"# Parent\n")
        framework_target = self.root / ".gitignore"
        framework_target.write_bytes(OURS)
        bases = UpdateBases(self.root)
        bases.advance("framework", ".gitignore", BASE)
        bases.write()
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        should_fail = True

        def validate(_root, **_kwargs):
            self.assertEqual(framework_target.read_bytes(), MERGED)
            self.assertIn(b"Parent", self.target.read_bytes())
            if should_fail:
                raise ValueError("reject complete prospective project")
            return {}

        def framework_adapter(**kwargs):
            return FilesystemProjectUpdateAdapter(origin_provider=origin, **kwargs)

        args = Namespace(
            path=self.root,
            apply=True,
            adopt_added=False,
            record_work_items=False,
            review_conflicts=False,
        )
        with (
            patch(
                "literate_ai.cli.project._repository_fetch_provider",
                return_value=SimpleNamespace(
                    deadline_evidence={}, update_blobs=lambda *_: {}
                ),
            ),
            patch(
                "literate_ai.cli.project.resolve_repository_lineage",
                return_value=self.catalogs.lineage,
            ),
            patch(
                "literate_ai.cli.project.plan_inherited_catalogs",
                side_effect=lambda *_: self.catalogs,
            ),
            patch(
                "literate_ai.cli.project.FilesystemProjectUpdateAdapter",
                side_effect=framework_adapter,
            ),
            patch(
                "literate_ai.adapters.project_updates._upstream_template",
                return_value={".gitignore": THEIRS},
            ),
            patch(
                "literate_ai.adapters.project_update_apply._upstream_template",
                return_value={".gitignore": THEIRS},
            ),
            patch(
                "literate_ai.adapters.project_update_conflicts._upstream_template",
                return_value={".gitignore": THEIRS},
            ),
            patch("literate_ai.cli.project.validate_project", side_effect=validate),
        ):
            from literate_ai.cli.errors import CliFailure

            with self.assertRaises(CliFailure):
                update_project_from_args(args)
            after = {
                path.relative_to(self.root): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            should_fail = False
            result = update_project_from_args(args)
        self.assertEqual(result["framework"]["applied"]["merged"], [".gitignore"])
        self.assertEqual(result["repository_lineage"]["applied"]["merged"], [self.path])
        self.assertTrue(self.target.read_bytes().endswith(b"Local policy\n"))


if __name__ == "__main__":
    unittest.main()
