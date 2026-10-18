"""Original case/skip authority and sealed reports cannot be replaced by exit zero."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_testing as testing
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestReceiptPolicy,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectProfile,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectCaseOutcome as Outcome,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectExpectedCase as Case,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectTestPolicy as Policy,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectTestReport as Report,
)


class RetainedProjectTestCoverageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.configured = ProjectTestReceiptPolicy(
            "original",
            "1.0.0",
            canonical_identity("original-runner"),
            ("test-report", "test-runner"),
            1,
        )
        self.policy = Policy(
            self.configured.identity,
            (Case("core", "tests"),),
            (("tests", "results/report.json"),),
        )
        self.profile = RetainedProjectProfile(
            "host",
            *(canonical_identity(n) for n in range(5)),
            (("tool", "tools/checker", canonical_identity("tool")),),
            (
                RetainedProjectAction(
                    "build", "build", "tool", (), (), ("app",), ("app",), 10
                ),
                RetainedProjectAction(
                    "tests", "test", "tool", (), ("build",), ("core",), ("results",), 10
                ),
                RetainedProjectAction(
                    "docs", "docs", "tool", (), ("build",), ("manual",), ("manual",), 10
                ),
                RetainedProjectAction(
                    "accept",
                    "accept",
                    "tool",
                    (),
                    ("tests", "docs"),
                    ("oracle",),
                    (),
                    10,
                ),
            ),
            ("app",),
        )
        self.write_policy(canonical_json_bytes(self.policy.to_dict()))

    def write_policy(self, content):
        (self.root / "policy.json").write_bytes(content)
        self.manifest = discover_retained_project_inputs(
            self.root,
            {"policy.json": "test-policy"},
            RetainedProjectLimits(20, 65536, 65536),
        )
        self.profile = replace(
            self.profile,
            test_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + self.manifest.members[0].sha256
            ),
        )

    def read(self, configured=None):
        return testing.read_retained_project_test_policy(
            self.root,
            self.manifest,
            self.profile,
            self.configured if configured is None else configured,
        )

    def test_policy_must_bind_actual_configured_runner_and_minimum(self):
        self.assertEqual(self.read(), self.policy)
        for configured in (
            replace(self.configured, runner_identity=canonical_identity("another")),
            replace(self.configured, minimum_test_count=2),
        ):
            with (
                self.subTest(configured=configured),
                self.assertRaisesRegex(ValueError, "project-policy-mismatch"),
            ):
                self.read(configured)

    def test_action_inventory_and_report_producer_must_be_complete(self):
        for policy in (
            replace(self.policy, cases=(Case("missing", "tests"),)),
            replace(self.policy, cases=(Case("core", "other"),)),
            replace(self.policy, reports=(("other", "results/report.json"),)),
            replace(self.policy, reports=(("tests", "app"),)),
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                policy.require_profile(self.profile, self.configured)

    def test_permitted_skips_never_satisfy_minimum_actual_passes(self):
        policy = replace(
            self.policy, cases=(Case("core", "tests", "platform restriction"),)
        )
        report = Report("tests", (Outcome("core", "skipped", "platform restriction"),))
        policy.require_report(report)
        self.assertEqual((report.passed, report.skipped), (0, 1))
        with self.assertRaisesRegex(ValueError, "passes-below-project-minimum"):
            policy.require_complete((report,), self.configured)

    def test_skip_permission_requires_exact_reviewed_reason(self):
        policy = replace(
            self.policy, cases=(Case("core", "tests", "platform restriction"),)
        )
        with self.assertRaisesRegex(ValueError, "skip-not-authorized"):
            policy.require_report(
                Report("tests", (Outcome("core", "skipped", "baseline exception"),))
            )

    def test_complete_pass_and_explicit_skip_remain_distinct(self):
        policy = replace(
            self.policy,
            cases=(
                Case("core", "tests"),
                Case("optional", "tests", "platform restriction"),
            ),
        )
        report = Report(
            "tests",
            (
                Outcome("core", "passed"),
                Outcome("optional", "skipped", "platform restriction"),
            ),
        )
        actions = list(self.profile.actions)
        actions[1] = replace(actions[1], inventory=("core", "optional"))
        policy.require_profile(
            replace(self.profile, actions=tuple(actions)), self.configured
        )
        policy.require_complete((report,), self.configured)
        self.assertEqual((report.passed, report.skipped), (1, 1))
        with self.assertRaisesRegex(ValueError, "incomplete-or-duplicated"):
            policy.require_complete((report, report), self.configured)

    def test_case_and_wire_aliases_refuse(self):
        for cases in (
            (Case("core", "tests"), Case("core", "tests")),
            (Case("z", "tests"), Case("a", "tests")),
        ):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                replace(self.policy, cases=cases)
        for path in (
            "../report.json",
            "C:report",
            "results/CON",
            "results\\report.json",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                replace(self.policy, reports=(("tests", path),))

    def test_baseline_exception_cannot_be_reported_as_passed(self):
        for outcome in ("xfail", "expected-failure", "ignored", "todo"):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                Outcome("core", outcome)
        with self.assertRaisesRegex(ValueError, "case-failed"):
            self.policy.require_report(Report("tests", (Outcome("core", "failed"),)))

    def test_hash_review_does_not_admit_duplicate_json_fields(self):
        document = canonical_json_bytes(self.policy.to_dict())
        document = document[:-1] + b',"cases":[]}'
        self.write_policy(document)
        with self.assertRaisesRegex(ValueError, "document-invalid"):
            self.read()

    def test_policy_metadata_reads_are_bounded(self):
        with patch.object(testing, "_MAXIMUM_DOCUMENT_BYTES", 64):
            with self.assertRaises(ValueError):
                self.read()

    def test_sealed_report_mutation_refuses_before_semantic_admission(self):
        outputs = self.root / "outputs"
        (outputs / "results").mkdir(parents=True)
        path = outputs / "results/report.json"
        report = Report("tests", (Outcome("core", "passed"),))
        path.write_bytes(canonical_json_bytes(report.to_dict()))
        manifest = discover_retained_project_inputs(
            outputs,
            {"results": "retained-output"},
            RetainedProjectLimits(20, 65536, 65536),
        )
        values = dict(
            action_id="tests",
            plan_identity=canonical_identity("plan"),
            observation_identity=canonical_identity("command"),
        )
        verified = testing.read_retained_project_test_report(
            outputs, manifest, self.policy, **values
        )
        self.assertEqual(verified.report, report)
        self.assertNotEqual(verified.identity, verified.observation_identity)
        path.write_bytes(
            canonical_json_bytes(
                Report("tests", (Outcome("core", "failed"),)).to_dict()
            )
        )
        with self.assertRaisesRegex(ValueError, "document-changed"):
            testing.read_retained_project_test_report(
                outputs, manifest, self.policy, **values
            )


if __name__ == "__main__":
    unittest.main()
