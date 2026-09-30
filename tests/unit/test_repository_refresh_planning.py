"""Reviewed refresh planning and refusal contracts over real Git repositories."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import repository_publication as publication
from literate_ai.adapters import repository_refresh_planning as planning
from literate_ai.adapters import repository_refresh_publication as refresh_publication
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_lineage import RepositoryFetchDeadlinePolicy
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.unit import test_refresh_file_custody as fixtures
from tests.unit.test_repository_orchestration import git, snapshot


class RepositoryRefreshPlanningTests(unittest.TestCase):
    def setUp(self):
        harness = fixtures.RefreshFileCustodyTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.harness = harness
        self.source = harness.fixture
        self.policy = RepositoryFetchDeadlinePolicy(120, 60, 10)

    def request(self, commit):
        return RepositoryRefreshRequest((RepositoryRefreshTarget("app", commit),))

    def assert_refuses(self, suffix, action):
        with self.assertRaises(OrchestrationInventoryError) as caught:
            action()
        self.assertEqual(caught.exception.code, "orchestration." + suffix)

    def test_plan_check_stale_and_acknowledgement_are_deterministic_and_nonmutating(
        self,
    ):
        target = self.harness.publish("source.txt", b"planned\n")
        request = self.request(target)
        before = snapshot(self.source.base)
        plan = planning.plan_repository_refresh(
            self.source.root, request, deadline_policy=self.policy
        )
        self.assertEqual(plan["schema"], planning.PLAN_SCHEMA)
        self.assertEqual(
            plan["plan_identity"],
            canonical_identity(
                {key: value for key, value in plan.items() if key != "plan_identity"}
            ).uri,
        )
        self.assertEqual(plan["request"], request.to_dict())
        self.assertEqual(plan["request_identity"], request.identity)
        self.assertEqual(plan["publication"], "verified")
        self.assertTrue(plan["apply_supported"])
        self.assertFalse(plan["writes"])
        self.assertTrue(plan["changed"])
        self.assertTrue(plan["source_writes"])
        self.assertEqual(
            plan["target_modes"], [{"path": "app", "mode": "source-transition"}]
        )
        self.assertTrue(plan["authority_review_required"])
        self.assertEqual(plan["child_authority"], "independent")
        self.assertEqual(plan["child_acceptance"], "not-qualified")
        self.assertEqual(plan["crash_replay"], "not-supported")
        checked = planning.check_repository_refresh(
            self.source.root,
            request,
            expected_plan_identity=plan["plan_identity"],
            deadline_policy=self.policy,
        )
        self.assertEqual(checked["schema"], planning.CHECK_SCHEMA)
        self.assertEqual(checked["state"], "current")
        self.assertTrue(checked["source_writes"])
        self.assertEqual(checked["target_modes"], plan["target_modes"])
        self.assertEqual(snapshot(self.source.base), before)
        self.assert_refuses(
            "refresh_plan_stale",
            lambda: planning.check_repository_refresh(
                self.source.root,
                request,
                expected_plan_identity="sha256:" + "0" * 64,
                deadline_policy=self.policy,
            ),
        )
        self.assert_refuses(
            "refresh_acknowledgement_required",
            lambda: planning.apply_planned_repository_refresh(
                self.source.root,
                request,
                expected_plan_identity=plan["plan_identity"],
                acknowledged=False,
                deadline_policy=self.policy,
            ),
        )
        self.assertEqual(snapshot(self.source.base), before)

    def test_exact_head_mode_is_bound_without_platform_tree_capture(self):
        target = self.harness.publish("source.txt", b"exact head\n")
        request = self.request(target)
        source_plan = planning.plan_repository_refresh(
            self.source.root, request, deadline_policy=self.policy
        )
        self.assertEqual(
            source_plan["target_modes"],
            [{"path": "app", "mode": "source-transition"}],
        )
        self.assertTrue(source_plan["source_writes"])
        git(self.source.child, "checkout", "-q", target)
        with patch.object(
            planning,
            "capture_published_repository_tree",
            side_effect=AssertionError("exact-head planning must not capture source"),
        ):
            root_only = planning.plan_repository_refresh(
                self.source.root, request, deadline_policy=self.policy
            )
        self.assertEqual(
            root_only["target_modes"], [{"path": "app", "mode": "root-pin-only"}]
        )
        self.assertFalse(root_only["source_writes"])
        self.assertNotEqual(root_only["plan_identity"], source_plan["plan_identity"])

    def test_exact_branch_witness_plan_identity_survives_shallow_fallback(self):
        target = self.harness.publish("source.txt", b"exact witness\n")
        git(self.source.child, "checkout", "-q", target)
        git(
            self.source.remote,
            "update-ref",
            "refs/changes/00/competing",
            target,
        )
        request = self.request(target)
        direct = planning.plan_repository_refresh(
            self.source.root, request, deadline_policy=self.policy
        )
        failed = False
        fetches = []
        proofs = []

        def runner(command, **kwargs):
            nonlocal failed
            if "fetch" in command:
                fetches.append(command)
                if "--depth=1" in command and not failed:
                    failed = True
                    return BoundedProcessResult(128, b"secret", b"secret")
            return publication.run_bounded_process(command, **kwargs)

        def forced(endpoint, commit, **options):
            proof = publication.verify_repository_publication(
                endpoint,
                commit,
                process_runner=runner,
                **options,
            )
            proofs.append(proof)
            return proof

        with patch.object(
            refresh_publication,
            "verify_repository_publication",
            side_effect=forced,
        ):
            fallback = planning.plan_repository_refresh(
                self.source.root, request, deadline_policy=self.policy
            )

        self.assertTrue(failed)
        self.assertEqual([proof.reference for proof in proofs], ["refs/heads/main"])
        self.assertEqual(len(fetches), 2)
        self.assertIn("--depth=1", fetches[0])
        self.assertIn("+refs/*:refs/litai-publication/*", fetches[1])
        self.assertEqual(fallback, direct)
        self.assertEqual(fallback["plan_identity"], direct["plan_identity"])

    def test_unpublished_and_dirty_children_refuse_without_root_writes(self):
        unpublished = self.source.advance(publish=False)
        before = snapshot(self.source.base)
        self.assert_refuses(
            "publication_unpublished",
            lambda: planning.plan_repository_refresh(
                self.source.root,
                self.request(unpublished),
                deadline_policy=self.policy,
            ),
        )
        self.assertEqual(snapshot(self.source.base), before)
        (self.source.child / "source.txt").write_bytes(b"foreign dirty work\n")
        before = snapshot(self.source.base)
        self.assert_refuses(
            "refresh_child_dirty",
            lambda: planning.plan_repository_refresh(
                self.source.root,
                self.request(self.source.pin),
                deadline_policy=self.policy,
            ),
        )
        self.assertEqual(snapshot(self.source.base), before)

    def test_mocked_windows_symlink_selection_is_not_reported_applicable(self):
        target = self.harness.publish("source.txt", b"windows prospective\n")
        proof = None

        def captured(_endpoint, _commit, **_options):
            return SimpleNamespace(
                publication=proof,
                tree=SimpleNamespace(entries=(SimpleNamespace(mode="120000"),)),
            )

        with (
            patch.object(planning, "_is_windows", return_value=True),
            patch.object(planning, "PublishedRepositoryTree", SimpleNamespace),
            patch.object(
                planning,
                "capture_published_repository_tree",
                side_effect=captured,
            ),
            patch.object(
                planning,
                "_application_support",
                wraps=planning._application_support,
            ) as support,
        ):
            original = planning.prepare_refresh_publication

            def remember(prepared, **options):
                nonlocal proof
                publication = original(prepared, **options)
                proof = publication.observations[0]
                return publication

            with patch.object(
                planning, "prepare_refresh_publication", side_effect=remember
            ):
                plan = planning.plan_repository_refresh(
                    self.source.root,
                    self.request(target),
                    deadline_policy=self.policy,
                )
        support.assert_called_once()
        self.assertFalse(plan["apply_supported"])
        self.assertEqual(
            plan["apply_unavailable_reason"],
            "prospective-symlink-unsupported-on-windows",
        )

    def test_mocked_windows_directory_transition_is_not_reported_applicable(self):
        target = self.harness.publish("source.txt", b"windows prospective\n")
        proof = None

        def captured(_endpoint, _commit, **_options):
            return SimpleNamespace(
                publication=proof,
                tree=SimpleNamespace(
                    entries=(SimpleNamespace(path="new-directory", mode="040000"),)
                ),
            )

        with (
            patch.object(planning, "_is_windows", return_value=True),
            patch.object(planning, "PublishedRepositoryTree", SimpleNamespace),
            patch.object(
                planning,
                "capture_published_repository_tree",
                side_effect=captured,
            ),
        ):
            original = planning.prepare_refresh_publication

            def remember(prepared, **options):
                nonlocal proof
                publication = original(prepared, **options)
                proof = publication.observations[0]
                return publication

            with patch.object(
                planning, "prepare_refresh_publication", side_effect=remember
            ):
                plan = planning.plan_repository_refresh(
                    self.source.root,
                    self.request(target),
                    deadline_policy=self.policy,
                )
        self.assertFalse(plan["apply_supported"])
        self.assertEqual(
            plan["apply_unavailable_reason"],
            "directory-transition-unsupported-on-windows",
        )


if __name__ == "__main__":
    unittest.main()
