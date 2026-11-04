"""A retained run is promoted only when re-verified against the project policy.

Invariant protected: a project test receipt derived from a retained-project run can
only exist for an unchanged, untampered, passing run whose plan, runner, suite,
evidence kinds and test count satisfy the project's exact receipt policy.
"""

import hashlib
import os
import shutil
import unittest
from dataclasses import replace
from unittest import mock

from literate_ai import test_receipts
from literate_ai.adapters import retained_receipt_promotion as retained_promotion
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.adapters.retained_project_runtime import (
    execute_local_retained_project,
)
from literate_ai.adapters.retained_receipt_promotion import (
    RetainedReceiptPromotionError,
    promote_retained_run,
    retained_project_runner_identity,
    verify_retained_run,
)
from literate_ai.cache_directories import CacheDirectories
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.retained_project_tests import RetainedProjectTestPolicy
from literate_ai.contracts.testing import (
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
)
from literate_ai.storage.cas import FileSystemCAS
from tests.critical import test_retained_project_completion as completed
from tests.critical import test_retained_project_execution as commands

KINDS = (
    "acceptance-result",
    "build-result",
    "resolved-sbom",
    "source-sbom",
    "test-report",
    "test-runner",
    "workspace-admission",
)


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedReceiptPromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.completion = completed.RetainedProjectCompletionTests()
        self.completion.setUp()
        self.addCleanup(self.completion.doCleanups)
        f = self.f = self.completion.fixture
        self.locks = self.completion.locks
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        # The project policy names the runner derived from the run's own test
        # actions; its bytes reach the manifest through the test-policy member.
        runner = retained_project_runner_identity(f.plan, f.manifest)
        self.policy = ProjectTestReceiptPolicy(
            "retained-fixture", "1.0.0", runner, KINDS, 1
        )
        test_policy = RetainedProjectTestPolicy(
            self.policy.identity,
            f.test_policy.cases,
            f.test_policy.reports,
        )
        content = canonical_json_bytes(test_policy.to_dict())
        (f.source / "test-policy.json").write_bytes(content)
        f.manifest = discover_retained_project_inputs(
            f.source,
            {"src": "source", "tools": "toolchain", "test-policy.json": "test-policy"},
            RetainedProjectLimits(100, 64 * 1024 * 1024, 128 * 1024 * 1024),
            executable_paths=(f.runner_path,),
        )
        profile = replace(
            f.plan.profile,
            worker_identity=worker.identity,
            test_policy_identity=canonical_identity_from_bytes(content),
        )
        f.plan = replace(f.plan, profile=profile, manifest_identity=f.manifest.identity)
        f.current = f.plan
        f.configured_test_policy = self.policy
        self.assertEqual(retained_project_runner_identity(f.plan, f.manifest), runner)
        self.planning = self.planning_for(f.plan)
        directories = CacheDirectories(
            f.source, f.base / "generated", f.base / "objects"
        )
        admission, _workspace = execute_local_retained_project(
            f.source,
            f.manifest,
            self.planning,
            component_locks=self.locks,
            worker=worker,
            current_plan=lambda: f.current,
            test_receipt_policy=self.policy,
            cache_directories=directories,
            authorization=f.plan.identity.uri,
            allow_host_execution=True,
        )
        self.admission = admission
        self.store = FileSystemCAS(admission.store.root, create=False)
        self.lock_ids = tuple(
            sorted((lock.identity for lock in self.locks), key=lambda i: i.uri)
        )
        self.revision = canonical_identity("project-revision")

    def planning_for(self, plan):
        return RetainedProjectPlanningResult(
            plan,
            project_retained_project_managed_graph(plan, self.locks, version="1.0.0"),
        )

    def verify(self, **changes):
        arguments = {
            "source": self.f.source,
            "manifest": self.f.manifest,
            "planning": self.planning,
            "component_locks": self.locks,
            "test_receipt_policy": self.policy,
            **changes,
        }
        return verify_retained_run(
            self.store, self.admission.receipt.identity, **arguments
        )

    def promote(self, verified=None, policy=None):
        return promote_retained_run(
            verified or self.verify(),
            project_id="fixture",
            project_revision_identity=self.revision,
            component_lock_identities=self.lock_ids,
            receipt_policy=policy or self.policy,
        )

    def refused(self, code, call):
        with self.assertRaises(RetainedReceiptPromotionError) as raised:
            call()
        self.assertEqual(raised.exception.code, "retained_promotion." + code)

    def test_verified_run_yields_a_candidate_satisfying_the_policy(self):
        candidate = self.promote()
        loaded = ProjectTestReceiptFinalizedCandidate.from_dict(candidate.to_dict())
        receipt = loaded.receipt
        evidence = {item.kind: item.identity for item in receipt.evidence}
        self.assertTrue(set(KINDS) <= set(evidence))
        self.assertEqual(evidence["test-runner"], self.policy.runner_identity)
        self.assertEqual(receipt.subject_identity, self.admission.receipt.identity)
        self.assertEqual(receipt.suite.identifier, self.policy.suite_id)
        self.assertEqual(receipt.summary.total, 1)
        self.assertEqual(loaded.component_lock_identities, self.lock_ids)
        # The exact check `project test-receipt update` applies to the receipt.
        self.assertIsNone(test_receipts._policy_mismatch(self.policy, receipt))

    def test_stale_plan_is_refused(self):
        f = self.f
        actions = list(f.plan.profile.actions)
        actions[0] = replace(actions[0], timeout_seconds=11)
        plan = replace(f.plan, profile=replace(f.plan.profile, actions=tuple(actions)))
        self.refused(
            "plan_stale", lambda: self.verify(planning=self.planning_for(plan))
        )
        self.refused(
            "plan_stale",
            lambda: self.verify(expected_plan_identity=canonical_identity("old")),
        )

    def test_changed_inputs_and_policy_are_refused(self):
        (self.f.source / "src/main.txt").write_bytes(b"changed")
        self.refused("inputs_changed", self.verify)
        (self.f.source / "src/main.txt").write_bytes(b"original\x00asset")
        other = replace(self.policy, minimum_test_count=2)
        self.refused("policy_stale", lambda: self.verify(test_receipt_policy=other))

    def test_wrong_runner_suite_evidence_and_count_are_refused(self):
        verified = self.verify()
        self.refused(
            "runner_mismatch",
            lambda: self.promote(
                verified, replace(self.policy, runner_identity=canonical_identity("x"))
            ),
        )
        self.refused(
            "suite_unsupported",
            lambda: self.promote(
                verified, replace(self.policy, suite_id="retained-legacy-parity")
            ),
        )
        self.refused(
            "evidence_missing",
            lambda: self.promote(
                verified,
                replace(
                    self.policy,
                    required_evidence_kinds=tuple(sorted((*KINDS, "package-result"))),
                ),
            ),
        )
        self.refused(
            "test_count_insufficient",
            lambda: self.promote(verified, replace(self.policy, minimum_test_count=2)),
        )

    def test_tampered_receipt_or_record_is_refused(self):
        digest = None
        for reference in self.admission.receipt.records:
            if reference.media_type == "application/json":
                digest = reference
                break
        path = self.store.path_for(digest)
        path.chmod(0o600)
        original = path.read_bytes()
        path.write_bytes(original.replace(b"{", b"{ ", 1))
        self.refused("custody_invalid", self.verify)
        path.write_bytes(original)
        receipt = self.store.path_for(
            type(digest)(self.admission.receipt.identity.digest, 0)
        )
        receipt.chmod(0o600)
        receipt.write_bytes(b"{}")
        self.refused("receipt_unreadable", self.verify)

    def test_non_object_observation_record_is_refused_cleanly(self):
        real = retained_promotion._read

        def read(store, reference, what):
            if what.startswith("sha256:"):
                return []
            return real(store, reference, what)

        with mock.patch.object(retained_promotion, "_read", read):
            self.refused("run_invalid", self.verify)


def canonical_identity_from_bytes(content):
    from literate_ai.contracts import ContentIdentity

    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    unittest.main()
