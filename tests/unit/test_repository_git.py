"""Real Git source locking through a local fixture transport, without a build."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.generation_preparation import (
    GenerationPreparationError,
    _component_definition,
)
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.source.repository_git import (
    GitRepositorySourceAcquirer,
    GitRepositorySourceCapturer,
)
from literate_ai.application.component_execution_planning import (
    ComponentExecutionPlanningError,
    plan_component_execution,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.application.repository_sources import (
    RepositorySourceLockResolver,
    RepositorySourceResolutionError,
    RepositorySourceResolver,
)
from literate_ai.contracts import RepositoryRevisionKind, RepositoryRevisionSelector
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from tests.unit.test_cli_component_locks import _run
from tests.unit.test_component_lock_planning import _fixture
from tests.unit.test_repository_sources import (
    _Authorizer,
    _Builder,
    _Cache,
    _Indexer,
    _Planner,
    dependency,
    identity,
)


@unittest.skipUnless(shutil.which("git"), "Git is required")
class RepositoryGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vendor_directory = tempfile.TemporaryDirectory(
            prefix="original source ", dir=self.root
        )
        self.addCleanup(self.vendor_directory.cleanup)
        self.vendor = Path(self.vendor_directory.name)
        self.git("init", "--template=", "-b", "main")
        self.git("config", "user.name", "Source fixture")
        self.git("config", "user.email", "fixture@example.test")
        (self.vendor / "build.txt").write_bytes(b"original native source\n")
        self.commit = self.commit_source()
        self.commands: list[tuple[str, ...]] = []

        def local_transport(command, **kwargs):
            command = tuple(command)
            self.commands.append(command)
            # Only the transport is substituted. Fetch, checkout, capture, hashing,
            # detached revision selection and cleanup all use the actual Git tools.
            command = tuple(
                self.vendor.as_uri() if value == dependency().repository_url else value
                for value in command
            )
            return run_bounded_process(command, **kwargs)

        transport = patch(
            "literate_ai.adapters.source.repository_git.run_bounded_process",
            side_effect=local_transport,
        )
        transport.start()
        self.addCleanup(transport.stop)
        self.acquirer = GitRepositorySourceAcquirer(scratch_root=self.root)
        self.capturer = GitRepositorySourceCapturer()
        self.resolver = RepositorySourceLockResolver(
            acquirer=self.acquirer, capturer=self.capturer
        )

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(self.vendor), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit_source(self) -> str:
        self.git("add", "build.txt")
        self.git("commit", "-m", "fixture source")
        return self.git("rev-parse", "HEAD")

    def admission_resolver(self, events: list[str]) -> RepositorySourceResolver:
        return RepositorySourceResolver(
            acquirer=self.acquirer,
            capturer=self.capturer,
            indexer=_Indexer(events),
            planner=_Planner(events),
            authorizer=_Authorizer(events),
            builder=_Builder(events),
            cache=_Cache(events),
        )

    def test_exact_lock_is_reproducible_and_cleans_temporary_source(self) -> None:
        selected = dependency()
        first = self.resolver.lock(selected)
        self.assertEqual(first, self.resolver.lock(selected))
        self.assertEqual(first.resolved_commit, self.commit)
        self.assertEqual(first.dependency, selected)
        self.assertEqual(list(self.root.glob("litai-source-*")), [])
        self.assertTrue(
            all(Path(command[0]).name.startswith("git") for command in self.commands)
        )

    def test_moved_branch_does_not_retarget_locked_admission(self) -> None:
        first = self.resolver.lock(dependency())
        (self.vendor / "build.txt").write_bytes(b"changed source\n")
        second_commit = self.commit_source()
        self.assertEqual(
            self.resolver.lock(dependency()).resolved_commit, second_commit
        )
        events: list[str] = []
        admitted = self.admission_resolver(events).resolve(
            dependency(),
            expected_lock=first,
            effective_revision=identity("effective"),
            flavor_set=identity("flavors"),
            toolchains=(identity("cmake"),),
        )
        self.assertEqual(admitted.lock, first)
        self.assertIn("build", events)

    def test_changed_lock_refuses_before_index_or_build(self) -> None:
        first = self.resolver.lock(dependency())
        for bad in (
            replace(first, source_tree=identity("forged tree")),
            replace(first, source_snapshot=identity("forged snapshot")),
            replace(first, resolver=identity("another resolver")),
        ):
            with self.subTest(lock=bad.identity.uri):
                events: list[str] = []
                with self.assertRaises(RepositorySourceResolutionError) as raised:
                    self.admission_resolver(events).resolve(
                        dependency(),
                        expected_lock=bad,
                        effective_revision=identity("effective"),
                        flavor_set=identity("flavors"),
                        toolchains=(identity("cmake"),),
                    )
                self.assertEqual(
                    raised.exception.code, "repository-source.lock-mismatch"
                )
                self.assertEqual(events, [])

    def test_missing_commit_fails_and_cleans_checkout(self) -> None:
        selected = dependency(
            RepositoryRevisionSelector(RepositoryRevisionKind.COMMIT, "f" * 40)
        )
        with self.assertRaises(RepositorySourceResolutionError):
            self.resolver.lock(selected)
        self.assertEqual(list(self.root.glob("litai-source-*")), [])

    def test_default_selector_resolves_actual_remote_head(self) -> None:
        selected = dependency(
            RepositoryRevisionSelector(RepositoryRevisionKind.DEFAULT, None)
        )
        self.assertEqual(self.resolver.lock(selected).resolved_commit, self.commit)

    def test_ambient_git_directory_and_filter_settings_cannot_change_capture(
        self,
    ) -> None:
        (self.vendor / ".gitattributes").write_text(
            "build.txt filter=fixture\n", encoding="utf-8"
        )
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "filter declaration")
        expected = self.resolver.lock(dependency())
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(self.root / "wrong-repository"),
                "GIT_WORK_TREE": str(self.root / "wrong-tree"),
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "filter.fixture.smudge",
                "GIT_CONFIG_VALUE_0": "command-that-must-not-run",
                "GIT_CONFIG_KEY_1": "filter.fixture.required",
                "GIT_CONFIG_VALUE_1": "true",
            },
        ):
            self.assertEqual(self.resolver.lock(dependency()), expected)

    def test_unresolved_lfs_refuses_without_build(self) -> None:
        (self.vendor / "build.txt").write_text(
            "version https://git-lfs.github.com/spec/v1\n"
            + "oid sha256:"
            + "a" * 64
            + "\nsize 100\n",
            encoding="utf-8",
        )
        self.commit_source()
        with self.assertRaises(RepositorySourceResolutionError) as raised:
            self.resolver.lock(dependency())
        self.assertEqual(raised.exception.code, "repository-source.lfs-unresolved")

    def project_with_source(self):
        component, flavors = _fixture(self.root / "project")
        contract = component / "integration.md"
        contract.write_text("Use only the public vendor SDK.\n", encoding="utf-8")
        path = component / "component.md"
        document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
        selected = dependency().to_dict()
        selected.pop("schema")
        selected["integration_contract"] = {
            "kind": "integration-contract",
            "uri": "integration.md",
            "pin": None,
        }
        document["source_dependencies"] = [selected]
        path.write_bytes(render_authoring_markdown(document, body))
        return component, flavors, contract

    def test_filesystem_planner_locks_actual_source_and_integration_bytes(self) -> None:
        component, flavors, contract = self.project_with_source()
        planner = FilesystemComponentLockPlanner(
            repository_source_resolver=self.resolver
        )
        plan, snapshot = planner.plan_with_snapshot(
            component,
            target_name="host",
            flavor_selectors=("+python", "+macos"),
            flavor_roots=(flavors,),
        )
        result = ComponentLockResolver().resolve(
            plan, expected_input_evidence_identity=plan.identity
        )
        source = result.lock.nodes[0].revision.repository_sources[0]
        self.assertEqual(source.resolved_commit, self.commit)
        self.assertIsNotNone(source.dependency.integration_contract)
        with self.assertRaises(ComponentExecutionPlanningError) as refusal:
            plan_component_execution(result.lock, model_identities={})
        self.assertEqual(
            refusal.exception.code,
            "component_plan.repository_source_admission_required",
        )
        with self.assertRaises(GenerationPreparationError) as refusal:
            _component_definition(
                plan.nodes[0].authoring, result.lock.nodes[0].revision
            )
        self.assertEqual(
            refusal.exception.code, "generate.repository_source_admission_required"
        )
        status, report, _ = _run(
            [
                "lock",
                str(component),
                "--target",
                "host",
                "--flavor",
                "+python",
                "--flavor",
                "+macos",
                "--flavor-root",
                str(flavors),
            ]
        )
        self.assertEqual(status, 0, report)
        self.assertTrue((component / "component.lock.json").is_file())
        contract.write_text("Changed public contract.\n", encoding="utf-8")
        with self.assertRaises(ComponentLockPlanningError):
            snapshot.require_unchanged(nodes=plan.nodes)

    def test_admitted_snapshot_rechecks_local_authority_without_refetching(self):
        component, flavors, contract = self.project_with_source()
        status, report, _ = _run(
            [
                "lock",
                str(component),
                "--target",
                "host",
                "--flavor",
                "+python",
                "--flavor",
                "+macos",
                "--flavor-root",
                str(flavors),
            ]
        )
        self.assertEqual(status, 0, report)
        planner = FilesystemComponentLockPlanner(
            repository_source_resolver=self.resolver
        )
        reader = FilesystemLockedGenerationAuthorityReader(planner)
        arguments = dict(
            target_name="host",
            flavor_selectors=("+python", "+macos"),
            flavor_roots=(flavors,),
        )
        admitted = reader.read(component, **arguments)
        (source,) = admitted.authority.lock.nodes[0].revision.repository_sources
        (self.vendor / "build.txt").write_text("later upstream source\n")
        self.commit_source()
        with patch.object(
            self.acquirer, "acquire", side_effect=AssertionError("unexpected fetch")
        ) as fetch:
            admitted.require_unchanged()
            with self.assertRaises(ComponentLockPlanningError) as missing:
                planner.plan_snapshot(
                    admitted._catalog,
                    target_name="host",
                    flavor_selectors=("+python", "+macos"),
                    locked_repository_sources=(),
                )
            self.assertEqual(
                missing.exception.code,
                "component_lock.repository_source_replay_missing",
            )
            with self.assertRaises(ComponentLockPlanningError) as conflict:
                planner.plan_snapshot(
                    admitted._catalog,
                    target_name="host",
                    flavor_selectors=("+python", "+macos"),
                    locked_repository_sources=(
                        source,
                        replace(source, resolved_commit="b" * 40),
                    ),
                )
            self.assertEqual(
                conflict.exception.code, "component_lock.repository_source_conflict"
            )
            fetch.assert_not_called()
        # A new reader still resolves the authored branch and detects its new commit.
        with self.assertRaises(LockedGenerationAuthorityReaderError):
            reader.read(component, **arguments)
        # TemporaryDirectory handles Git's read-only objects on Windows. Remove
        # the original bytes, not only the authored URI or a renamed directory.
        self.vendor_directory.cleanup()
        self.assertFalse(self.vendor.exists())
        admitted.require_unchanged()
        contract.write_text("changed local interface\n")
        with self.assertRaises(LockedGenerationAuthorityReaderError):
            admitted.require_unchanged()
