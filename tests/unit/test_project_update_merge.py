"""End-to-end baseline, clean-merge, reviewed-resolution and rollback proofs."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.project_update_apply import apply_project_update
from literate_ai.adapters.project_update_conflicts import framework_conflict_diffs
from literate_ai.adapters.project_update_reviews import review_update_conflicts
from literate_ai.adapters.project_updates import (
    FilesystemProjectUpdateAdapter,
    ProjectUpdateError,
)
from literate_ai.adapters.update_merge import (
    CHECKPOINT,
    UpdateBases,
    digest,
    enrich_merge,
    load_resolutions,
    merge_text,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectUpdateClassification,
    ProjectUpdateFile,
)
from tests.unit.root_parent_adapter import RootParentProjectInitializationAdapter
from tests.support.fixtures_test_project_update_adapter import _origin

BASE = b"first\n" + b"context\n" * 10 + b"last\n"
OURS = BASE.replace(b"first", b"local first")
THEIRS = BASE.replace(b"last", b"upstream last")
MERGED = OURS.replace(b"last", b"upstream last")


class TextMergeTests(unittest.TestCase):
    def test_disjoint_and_identical_edits_preserve_line_endings(self):
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                base, ours, theirs, merged = [
                    v.replace(b"\n", newline) for v in (BASE, OURS, THEIRS, MERGED)
                ]
                self.assertEqual(merge_text(base, ours, theirs), merged)
                self.assertEqual(merge_text(base, ours, ours), ours)

    def test_overlaps_and_binary_inputs_are_not_written_as_markers(self):
        self.assertIsNone(
            merge_text(BASE, OURS, BASE.replace(b"first", b"other first"))
        )
        self.assertIsNone(merge_text(b"a\0", b"b\0", b"c\0"))
        self.assertIsNone(merge_text(b"\xff", b"a", b"b"))

    def test_missing_or_wrong_base_never_becomes_mergeable(self):
        item = ProjectUpdateFile(
            "a.md",
            ProjectUpdateClassification.CONFLICT,
            *(ContentIdentity.parse_uri(digest(v)) for v in (BASE, OURS, THEIRS)),
        )
        self.assertEqual(enrich_merge(item, None, OURS, THEIRS), item)
        self.assertEqual(enrich_merge(item, b"wrong", OURS, THEIRS), item)

    def test_historical_base_reads_exact_commit_without_checkout(self):
        from literate_ai.adapters.repository_lineage import (
            GitRepositorySnapshotProvider,
        )
        from literate_ai.contracts import RepositoryParentReference

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()

            def git(*args):
                return (
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            "commit.gpgsign=false",
                            "-c",
                            "core.hooksPath=" + str(root / "no-hooks"),
                            *args,
                        ],
                        cwd=source,
                        check=True,
                        capture_output=True,
                    )
                    .stdout.decode()
                    .strip()
                )

            git("init", "--quiet")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Merge fixture")
            (source / "base.txt").write_bytes(BASE)
            git("add", "base.txt")
            git("commit", "--quiet", "-m", "base")
            revision = git("rev-parse", "HEAD")
            (source / "base.txt").write_bytes(THEIRS)
            git("commit", "--quiet", "-am", "later")
            provider = GitRepositorySnapshotProvider(root / "cache")
            reference = RepositoryParentReference(source.as_uri(), revision)
            self.assertEqual(
                provider.update_blobs(reference, ("base.txt", "missing.txt")),
                {"base.txt": BASE},
            )
            self.assertEqual((source / "base.txt").read_bytes(), THEIRS)
            with self.assertRaises(ValueError):
                provider.update_blobs(reference, ("../escape",))
            with self.assertRaises(ValueError):
                provider.update_blobs(
                    RepositoryParentReference(source.as_uri(), "HEAD"), ("base.txt",)
                )


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

    def test_changed_checkpoint_rejects_an_otherwise_current_plan(self):
        plan = self.adapter.plan(self.root)
        bases = UpdateBases(self.root)
        bases.retain(b"different checkpoint observation")
        bases.write()
        with self.assertRaises(ProjectUpdateError) as caught:
            apply_project_update(plan, self.root)
        self.assertEqual(caught.exception.code, "project.update_base_changed")
        self.assertEqual(self.target.read_bytes(), OURS)

    def test_checkpoint_link_cannot_redirect_writes(self):
        checkpoint = self.root / CHECKPOINT
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(checkpoint.read_bytes())
        before = outside.read_bytes()
        checkpoint.unlink()
        try:
            checkpoint.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"host cannot create a symbolic link: {exc}")
        with self.assertRaises(ProjectUpdateError):
            self.adapter.plan(self.root)
        self.assertEqual(outside.read_bytes(), before)

    def test_reviewed_merge_is_bound_to_inputs_and_applied_explicitly(self):
        from types import SimpleNamespace

        self.upstream[".gitignore"] = BASE.replace(b"first", b"other first")
        plan = self.adapter.plan(self.root)
        with patch(
            "literate_ai.adapters.project_update_conflicts._upstream_template",
            return_value=self.upstream,
        ):
            diffs = framework_conflict_diffs(self.root, plan)
        self.assertEqual(diffs[0]["base"], BASE.decode())
        runner = SimpleNamespace(
            run_json_task=lambda *_args, **_kwargs: SimpleNamespace(
                response={
                    "decision": "merge",
                    "rationale": "retain both intended values",
                    "merged_text": "reviewed first\n",
                }
            )
        )
        reviews = review_update_conflicts(diffs, task_runner=runner)
        self.assertEqual(self.target.read_bytes(), OURS)
        source = Path(self.temporary.name) / "review.json"
        source.write_text(json.dumps({"conflict_reviews": reviews}))
        selected = load_resolutions(source, plan.files)
        applied = apply_project_update(plan, self.root, resolutions=selected)
        self.assertEqual(self.target.read_text(), "reviewed first\n")
        self.assertEqual(
            applied.resolutions[0]["rationale"], "retain both intended values"
        )
        with self.assertRaises(ProjectUpdateError):
            load_resolutions(source, self.adapter.plan(self.root).files)

    def test_tampered_base_fails_closed(self):
        path = self.root / CHECKPOINT
        document = json.loads(path.read_bytes())
        document["blobs"][digest(BASE)] = "d3Jvbmc="
        path.write_text(json.dumps(document))
        with self.assertRaises(ProjectUpdateError) as caught:
            self.adapter.plan(self.root)
        self.assertEqual(caught.exception.code, "project.update_base_invalid")

    def test_explicit_keep_local_and_take_upstream_advance_the_upstream_base(self):
        from literate_ai.adapters.project_update_reviews import CONFLICT_REVIEW_SCHEMA

        self.upstream[".gitignore"] = BASE.replace(b"first", b"different first")
        initial_checkpoint = (self.root / CHECKPOINT).read_bytes()
        for decision, expected in (
            ("keep-local", OURS),
            ("take-upstream", self.upstream[".gitignore"]),
        ):
            with self.subTest(decision=decision):
                (self.root / CHECKPOINT).write_bytes(initial_checkpoint)
                self.target.write_bytes(OURS)
                plan = self.adapter.plan(self.root)
                item = self.item(plan)
                review = {
                    "schema": CONFLICT_REVIEW_SCHEMA,
                    "path": item.path,
                    "file_identity": item.identity.to_dict(),
                    "decision": decision,
                    "rationale": "explicit operator choice",
                    "merged_text": None,
                    "applied": False,
                }
                source = Path(self.temporary.name) / "review.json"
                source.write_text(json.dumps([review]))
                choices = load_resolutions(source, plan.files)
                result = apply_project_update(plan, self.root, resolutions=choices)
                self.assertEqual(self.target.read_bytes(), expected)
                self.assertTrue(result.resolutions[0]["applied"])
                bases = UpdateBases(self.root)
                self.assertEqual(
                    bases.get(bases.identities("framework")[item.path]),
                    self.upstream[item.path],
                )
                source.write_text(json.dumps([review, review]))
                with self.assertRaises(ProjectUpdateError):
                    load_resolutions(source, plan.files)

    def test_recovery_accepts_only_recorded_base_hash(self):
        bases = UpdateBases(self.root)
        bases.blobs.pop(digest(BASE))
        from literate_ai.adapters.update_merge import recover_bases

        item = self.item(self.adapter.plan(self.root))
        item = replace(
            item,
            classification=ProjectUpdateClassification.CONFLICT,
            base_text=None,
            merged_text=None,
        )
        recovered = []

        def reader(reference, paths):
            recovered.append(reference.requested_revision)
            return {"wrong": b"wrong", "base": BASE}

        recover_bases(self.root, (item,), bases, reader, self.origin)
        self.assertEqual(recovered, [self.origin.git_revision])
        self.assertEqual(bases.get(item.baseline_identity), BASE)


class CatalogMergeTests(unittest.TestCase):
    def setUp(self):
        from literate_ai.adapters.project_initialization import (
            FilesystemProjectInitializationAdapter,
        )
        from literate_ai.adapters.repository_catalogs import InheritedCatalogPlan
        from literate_ai.adapters.repository_updates import (
            FilesystemRepositoryUpdateAdapter,
        )
        from tests.support.fixtures_test_repository_updates import fixture, flavor_item, origin

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

    def test_catalog_overlay_survives_consecutive_parent_updates(self):
        from literate_ai.contracts import CatalogImportsFile

        self.upstream(b"# Upstream first\n")
        plan = self.adapter.plan(self.root)
        file = next(file for file in plan.contract.files if file.path == self.path)
        self.assertEqual(file.classification, ProjectUpdateClassification.MERGEABLE)
        self.adapter.apply(self.root, plan, finalizer=lambda _: None)
        self.assertIn(b"Upstream first", self.target.read_bytes())
        self.assertTrue(self.target.read_bytes().endswith(b"Local policy\n"))
        # Merged local bytes must not be represented as an exact upstream import.
        self.assertFalse(
            any(
                file.path == self.path
                for imported in CatalogImportsFile.load(self.root).imports
                for file in imported.files
            )
        )
        self.upstream(b"# Upstream second\n")
        self.adapter.apply(
            self.root, self.adapter.plan(self.root), finalizer=lambda _: None
        )
        self.assertIn(b"Upstream second", self.target.read_bytes())
        self.assertNotIn(b"Upstream first", self.target.read_bytes())
        self.assertTrue(self.target.read_bytes().endswith(b"Local policy\n"))

    def test_failed_catalog_validation_restores_provenance_and_base(self):
        from literate_ai.adapters.repository_updates import RepositoryUpdateError

        self.upstream(b"# Upstream\n")
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

        def reject(_root):
            self.assertIn(b"Upstream", self.target.read_bytes())
            raise ValueError("reject merged catalog")

        with self.assertRaises(RepositoryUpdateError):
            self.adapter.apply(
                self.root, self.adapter.plan(self.root), finalizer=reject
            )
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_unresolved_conflict_keeps_recoverable_baseline_across_apply(self):
        self.upstream(b"# Parent\n")
        self.target.write_bytes(self.base.replace(b"\n", b"\n# Local\n", 1))
        first = self.adapter.plan(self.root)
        first_file = next(
            file for file in first.contract.files if file.path == self.path
        )
        self.assertEqual(
            first_file.classification, ProjectUpdateClassification.CONFLICT
        )
        self.adapter.apply(self.root, first, finalizer=lambda _: None)
        second_file = next(
            file
            for file in self.adapter.plan(self.root).contract.files
            if file.path == self.path
        )
        self.assertEqual(second_file, first_file)

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
