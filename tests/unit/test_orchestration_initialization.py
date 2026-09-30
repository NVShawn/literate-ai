"""Real-Git preservation and owned rollback for reviewed root initialization."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import orchestration_initialization as initialization
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.projects import CANONICAL_AGENT_SHIMS, discover_project
from tests.unit import test_orchestration_planning as planning_fixtures
from tests.unit.test_repository_orchestration import git, repository, snapshot


class OrchestrationInitializationTests(unittest.TestCase):
    def setUp(self):
        fixture = planning_fixtures.OrchestrationPlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base, self.declaration = (
            fixture.root,
            fixture.base,
            fixture.declaration,
        )
        (self.root / "README.md").write_bytes(b"Existing project README\n")

    def plan(self, **overrides):
        return initialization.plan_orchestration_initialization(
            self.root,
            self.declaration,
            project_id="super",
            version=overrides.get("version", "1.0.0"),
        )

    def initialize(self, plan=None, **overrides):
        plan = self.plan() if plan is None else plan
        options = dict(
            project_id="super",
            version="1.0.0",
            expected_plan_identity=plan["plan_identity"],
            acknowledged=True,
        )
        options.update(overrides)
        return initialization.initialize_orchestration(
            self.root, self.declaration, **options
        )

    def test_plan_is_read_only_and_binds_explicit_project_and_owned_files(self):
        before = snapshot(self.base)
        first = self.plan()
        self.assertEqual(self.plan(), first)
        self.assertFalse(first["writes"])
        self.assertFalse(first["execution"])
        self.assertTrue(first["apply_supported"])
        self.assertEqual(first["project"]["project_id"], "super")
        self.assertEqual(len(first["files"]), 7)
        self.assertNotEqual(
            first["plan_identity"], self.plan(version="1.1.0")["plan_identity"]
        )
        self.assertEqual(snapshot(self.base), before)

    def test_initialize_preserves_originals_and_publishes_manifest_last(self):
        before = snapshot(self.root)
        plan = self.plan()
        link = os.link
        published = []

        def observe(source, target, **kwargs):
            self.assertFalse((self.root / "literate.project.json").exists())
            published.append(Path(target).relative_to(self.root).as_posix())
            return link(source, target, **kwargs)

        with patch.object(initialization.os, "link", side_effect=observe):
            result = self.initialize(plan)
        self.assertEqual(result["state"], "initialized")
        self.assertEqual(result["cleanup_retained"], [])
        self.assertEqual(published[-1], "literate.project.json")
        after = snapshot(self.root)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(set(after) - set(before), set(result["created_files"]))
        self.assertEqual(
            discover_project(self.root).definition.identity.uri,
            result["project_identity"],
        )
        self.assertFalse((self.root / ".literate/.orchestration-stage").exists())

    def test_acknowledgement_is_required_before_inspection_or_mutation(self):
        for value in (False, None, 1, "yes"):
            with (
                self.subTest(value=value),
                patch.object(
                    initialization, "_prepare", side_effect=AssertionError("inspection")
                ),
            ):
                with self.assertRaises(OrchestrationInventoryError) as caught:
                    initialization.initialize_orchestration(
                        self.root,
                        self.declaration,
                        project_id="super",
                        version="1.0.0",
                        expected_plan_identity="sha256:" + "a" * 64,
                        acknowledged=value,
                    )
                self.assertEqual(
                    caught.exception.code, "orchestration.acknowledgement_required"
                )

    def test_stale_plan_refuses_without_writes(self):
        plan = self.plan()
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.initialize(plan, version="1.1.0")
        self.assertEqual(caught.exception.code, "orchestration.plan_stale")
        self.assertEqual(snapshot(self.base), before)

    def test_existing_targets_and_portable_aliases_refuse(self):
        for name in (
            "SKILL.md",
            "PROJECT.md",
            "literate.project.json",
            ".literate",
            "skill.MD",
            ".LITERATE",
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(b"foreign")
                before = snapshot(self.root)
                with self.assertRaises(OrchestrationInventoryError):
                    self.plan()
                self.assertEqual(snapshot(self.root), before)
                path.unlink()

    def test_existing_valid_agent_shims_are_preserved(self):
        for name, marker in CANONICAL_AGENT_SHIMS.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(marker + "\n", encoding="utf-8")
        before = snapshot(self.root)
        self.initialize()
        after = snapshot(self.root)
        self.assertEqual({path: after[path] for path in before}, before)

    def test_invalid_agent_shim_refuses(self):
        (self.root / "AGENTS.md").write_bytes(b"Unrelated onboarding")
        before = snapshot(self.root)
        with self.assertRaises(OrchestrationInventoryError):
            self.plan()
        self.assertEqual(snapshot(self.root), before)

    def test_shim_shaped_child_content_remains_independent(self):
        modules = self.root / ".gitmodules"
        modules.write_text(
            modules.read_text()
            + '\n[submodule "cursor"]\npath = .cursor\nurl = ../cursor.git\n',
            encoding="utf-8",
        )
        git(self.root, "add", ".gitmodules")
        commit = git(self.root, "rev-parse", "HEAD").decode().strip()
        git(
            self.root,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            commit,
            ".cursor",
        )
        child = self.root / ".cursor"
        repository(child)
        (child / "rules").mkdir()
        (child / "rules/literate-ai.mdc").write_bytes(b"independent child instructions")
        before = snapshot(child)
        self.initialize()
        self.assertEqual(snapshot(child), before)

    def test_flush_failure_rolls_back_only_new_additions(self):
        plan = self.plan()
        before = snapshot(self.root)
        with patch.object(initialization.os, "fsync", side_effect=OSError("fixture")):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.initialize(plan)
        self.assertEqual(caught.exception.code, "orchestration.initialization_failed")
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / ".literate").exists())

    def test_partial_stage_write_failure_removes_only_the_written_prefix(self):
        plan = self.plan()
        before = snapshot(self.root)
        write = os.write
        calls = 0

        def partial(descriptor, content):
            nonlocal calls
            calls += 1
            if calls == 1:
                return write(descriptor, content[:4])
            raise OSError("fixture partial write")

        with patch.object(initialization.os, "write", side_effect=partial):
            with self.assertRaises(OrchestrationInventoryError):
                self.initialize(plan)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / ".literate").exists())

    def test_destination_appearing_at_atomic_link_is_preserved(self):
        plan = self.plan()
        link = os.link

        def concurrent(source, target, **kwargs):
            if Path(target).name == "SKILL.md" and Path(target).parent == self.root:
                Path(target).write_bytes(b"foreign new onboarding")
            return link(source, target, **kwargs)

        with patch.object(initialization.os, "link", side_effect=concurrent):
            with self.assertRaises(OrchestrationInventoryError):
                self.initialize(plan)
        self.assertEqual(
            (self.root / "SKILL.md").read_bytes(), b"foreign new onboarding"
        )
        self.assertFalse((self.root / "literate.project.json").exists())
        self.assertFalse((self.root / ".literate").exists())

    def test_concurrent_declaration_change_rolls_back_root_but_preserves_change(self):
        plan = self.plan()
        before = snapshot(self.root)
        link = os.link

        def changed(source, target, **kwargs):
            self.declaration.write_text(
                json.dumps(
                    {"schema": "literate-ai/orchestration@1", "relationships": []}
                )
            )
            return link(source, target, **kwargs)

        with patch.object(initialization.os, "link", side_effect=changed):
            with self.assertRaises(OrchestrationInventoryError):
                self.initialize(plan)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(json.loads(self.declaration.read_text())["relationships"], [])

    def test_changed_published_file_is_preserved_and_rollback_is_reported_incomplete(
        self,
    ):
        plan = self.plan()
        validate = initialization.FilesystemProjectValidationAdapter.validate

        def changed(adapter, root, **options):
            result = validate(adapter, root, **options)
            if root == self.root:
                (root / "PROJECT.md").write_bytes(b"concurrent requirements")
            return result

        with patch.object(
            initialization.FilesystemProjectValidationAdapter, "validate", new=changed
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.initialize(plan)
        self.assertEqual(caught.exception.code, "orchestration.rollback_incomplete")
        self.assertEqual(
            (self.root / "PROJECT.md").read_bytes(), b"concurrent requirements"
        )
        self.assertFalse((self.root / "literate.project.json").exists())

    def test_initialized_dirty_child_source_and_git_metadata_are_unchanged(self):
        child = self.root / "app"
        repository(child)
        (child / "source.txt").write_bytes(b"Uncommitted child edit")
        (child / "literate.project.json").write_bytes(b"unrelated child authority")
        before = snapshot(child)
        self.initialize()
        self.assertEqual(snapshot(child), before)

    def test_linked_worktree_preserves_shared_git_metadata(self):
        original = self.root
        worktree = self.base / "linked"
        git(original, "worktree", "add", "--detach", str(worktree))
        before = snapshot(original)
        self.root = worktree
        self.initialize()
        self.assertEqual(snapshot(original), before)

    def test_public_initialization_requires_review_and_acknowledgement(self):
        options = ("--project-id", "super", "--project-version", "1.0.0")
        before = snapshot(self.root)
        code, envelope = self.fixture.invoke("plan", *options)
        self.assertEqual(code, 0, envelope)
        plan = envelope["result"]
        reviewed = (*options, "--expected-plan-identity", plan["plan_identity"])
        code, envelope = self.fixture.invoke("check", *reviewed)
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["state"], "current")
        code, envelope = self.fixture.invoke("initialize", *reviewed)
        self.assertNotEqual(code, 0, envelope)
        self.assertIn("orchestration.acknowledgement_required", str(envelope))
        self.assertEqual(snapshot(self.root), before)
        code, envelope = self.fixture.invoke("initialize", *reviewed, "--acknowledge")
        self.assertEqual(code, 0, envelope)
        result = envelope["result"]
        self.assertEqual(result["state"], "initialized")
        self.assertTrue(result["writes"])
        self.assertFalse(result["execution"])
        after = snapshot(self.root)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(set(after) - set(before), set(result["created_files"]))

    def test_independent_file_child_fixture_plans_initialization(self):
        child = self.base / "wheel-child"
        repository(child)
        child_remote = self.base / "wheel-child.git"
        git(
            self.base,
            "clone",
            "--bare",
            "--no-local",
            str(child),
            str(child_remote),
        )
        root = self.base / "wheel-super"
        root.mkdir()
        git(root, "init", "-b", "main")
        git(root, "config", "user.email", "refresh@example.invalid")
        git(root, "config", "user.name", "Wheel Refresh")
        (root / "README.md").write_text("# Wheel refresh\n", encoding="utf-8")
        (root / ".gitmodules").write_text(
            '[submodule "app"]\n'
            "\tpath = app\n"
            f"\turl = {child_remote.resolve().as_uri()}\n",
            encoding="utf-8",
            newline="\n",
        )
        git(
            self.base,
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--no-local",
            child_remote.resolve().as_uri(),
            str(root / "app"),
        )
        child_commit = git(root / "app", "rev-parse", "HEAD").decode().strip()
        git(root, "add", "README.md", ".gitmodules")
        git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            child_commit,
            "app",
        )
        git(root, "commit", "-m", "Pin child")
        root_remote = self.base / "wheel-super.git"
        git(root_remote.parent, "init", "--bare", str(root_remote))
        git(root, "remote", "add", "origin", root_remote.resolve().as_uri())
        declaration = self.base / "wheel-orchestration.json"
        declaration.write_text(
            '{"relationships":[],"schema":"literate-ai/orchestration@1"}\n',
            encoding="utf-8",
        )

        plan = initialization.plan_orchestration_initialization(
            root,
            declaration,
            project_id="super",
            version="1.0.0",
        )

        self.assertEqual(
            plan["project"]["repository_orchestration"]["relationships"], []
        )
        self.assertFalse(plan["writes"])

    def test_public_plan_requires_both_project_fields(self):
        before = snapshot(self.root)
        for options in (("--project-id", "super"), ("--project-version", "1.0.0")):
            with self.subTest(options=options):
                code, envelope = self.fixture.invoke("plan", *options)
                self.assertNotEqual(code, 0, envelope)
                self.assertIn("orchestration.project_required", str(envelope))
        self.assertEqual(snapshot(self.root), before)

    def test_reviewed_plan_is_portable_to_clean_shallow_clone(self):
        plan = self.plan()
        clone = self.base / "clone"
        git(self.base, "clone", "--depth", "1", self.root.as_uri(), str(clone))
        self.root = clone
        self.assertEqual(self.plan(), plan)
        self.assertEqual(self.initialize(plan)["state"], "initialized")

    def test_final_validation_failure_rolls_back_all_owned_additions(self):
        plan = self.plan()
        before = snapshot(self.root)
        validate = initialization.FilesystemProjectValidationAdapter.validate

        def refuse(adapter, root, **options):
            if root == self.root:
                raise ValueError("fixture final validation failure")
            return validate(adapter, root, **options)

        with patch.object(
            initialization.FilesystemProjectValidationAdapter, "validate", new=refuse
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.initialize(plan)
        self.assertEqual(caught.exception.code, "orchestration.initialization_failed")
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / ".literate").exists())

    def test_replaced_root_is_preserved_and_reports_incomplete_rollback(self):
        plan = self.plan()
        moved = self.base / "moved"

        def replace_root(source, target, **options):
            self.root.rename(moved)
            self.root.mkdir()
            (self.root / "README.md").write_bytes(b"replacement root")
            raise OSError("fixture concurrent root replacement")

        with patch.object(initialization.os, "link", side_effect=replace_root):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.initialize(plan)
        self.assertEqual(caught.exception.code, "orchestration.rollback_incomplete")
        self.assertEqual((self.root / "README.md").read_bytes(), b"replacement root")
        self.assertEqual(set(snapshot(self.root)), {"README.md"})
        self.assertTrue((moved / ".git").is_dir())
        self.assertTrue((moved / ".literate/.orchestration-stage").is_dir())

    def test_invalid_review_identity_refuses_before_inspection(self):
        for identity in (None, 1, "", "sha256:" + "A" * 64, "sha256:" + "a" * 63):
            with (
                self.subTest(identity=identity),
                patch.object(initialization, "_prepare", side_effect=AssertionError),
                self.assertRaises(OrchestrationInventoryError) as caught,
            ):
                self.initialize({"plan_identity": identity})
            self.assertEqual(
                caught.exception.code, "orchestration.plan_identity_invalid"
            )

    def test_public_plan_rejects_indirect_root_without_writes(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        self.fixture.root = alias
        before = snapshot(self.root)
        code, envelope = self.fixture.invoke(
            "plan", "--project-id", "super", "--project-version", "1.0.0"
        )
        self.assertNotEqual(code, 0, envelope)
        self.assertIn("orchestration.inputs_invalid", str(envelope))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / ".literate").exists())

    def test_replaced_owned_directory_is_not_removed_during_rollback(self):
        plan = self.plan()
        validate = initialization.FilesystemProjectValidationAdapter.validate
        retained = self.base / "original-metadata"

        def replace_metadata(adapter, root, **options):
            result = validate(adapter, root, **options)
            if root == self.root:
                (root / ".literate").rename(retained)
                (root / ".literate").mkdir()
                (root / ".literate/foreign.txt").write_bytes(b"concurrent metadata")
            return result

        with patch.object(
            initialization.FilesystemProjectValidationAdapter,
            "validate",
            new=replace_metadata,
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.initialize(plan)
        self.assertEqual(caught.exception.code, "orchestration.rollback_incomplete")
        self.assertEqual(
            (self.root / ".literate/foreign.txt").read_bytes(), b"concurrent metadata"
        )
        self.assertTrue((retained / ".orchestration-stage").is_dir())
        self.assertFalse((self.root / "literate.project.json").exists())
