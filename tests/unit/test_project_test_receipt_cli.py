"""CLI tests for the replaceable, Git-history-friendly project test receipt."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli import main
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
    rebuild_project_authority_identity,
)
from literate_ai.projects import load_project
from tests.support.fixtures_test_project_cli import (
    copy_generation_catalogs,
    copy_hello_component,
    refresh_authority_review,
)
from tests.unit.root_parent_adapter import root_parent_for_fixture_project

_FINALIZATION_EVIDENCE = {
    "lifecycle-command": canonical_identity({"fixture": "command"}),
    "lifecycle-request": canonical_identity({"fixture": "request"}),
    "source-cache-decision": canonical_identity({"fixture": "decision"}),
    "source-cache-lifecycle": canonical_identity({"fixture": "lifecycle"}),
}


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    with root_parent_for_fixture_project(arguments):
        status = main(arguments, stdout=output, stderr=errors)
    content = output.getvalue() if status == 0 else errors.getvalue()
    return status, json.loads(content)


def fixture_policy(version: str = "1.0.0") -> ProjectTestReceiptPolicy:
    return ProjectTestReceiptPolicy(
        suite_id="portable-e2e",
        suite_version=version,
        runner_identity=canonical_identity({"fixture": "trusted-test-runner"}),
        required_evidence_kinds=(
            "build-result",
            "generation-provenance",
            "observation-result",
            "security-scan-report",
            "test-report",
            "test-runner",
        ),
        minimum_test_count=4,
    )


def configure_receipt_policy(project_root: Path) -> ProjectTestReceiptPolicy:
    manifest_path = project_root / "literate.project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = fixture_policy(str(manifest["version"]))
    manifest["test_receipt_policy"] = policy.to_dict()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    refresh_authority_review(project_root)
    return policy


def make_candidate(
    project_root: Path,
    *,
    project_id: str | None = None,
    policy: ProjectTestReceiptPolicy | None = None,
) -> dict:
    project = load_project(project_root)
    policy = policy or project.definition.test_receipt_policy
    if policy is None:
        raise AssertionError("candidate fixture requires an explicit receipt policy")
    status, envelope = invoke("project", "documentation-review", str(project_root))
    if status != 0:
        raise AssertionError(envelope)
    project_revision_identity = ContentIdentity.parse_uri(
        str(envelope["result"]["authority_identity"])
    )
    evidence_by_kind = {
        kind: (
            policy.runner_identity
            if kind == "test-runner"
            else canonical_identity({"fixture": kind})
        )
        for kind in policy.required_evidence_kinds
    }
    evidence_by_kind.update(_FINALIZATION_EVIDENCE)
    receipt = ProjectTestReceipt(
        project_id=project_id or project.definition.project_id,
        project_revision_identity=project_revision_identity,
        subject_identity=canonical_identity({"fixture": "current-authority"}),
        suite=VersionedContentRef(
            "test-suite",
            policy.suite_id,
            policy.suite_version,
            canonical_identity({"fixture": "current-suite"}),
        ),
        outcome="passed",
        summary=ProjectTestSummary(
            policy.minimum_test_count,
            policy.minimum_test_count,
            0,
            0,
        ),
        result_identity=canonical_identity({"fixture": "passing-result"}),
        evidence=tuple(
            ProjectTestEvidence(kind, identity)
            for kind, identity in sorted(evidence_by_kind.items())
        ),
    )
    return receipt.to_dict()


def finalize_candidate(receipt_value: dict) -> dict:
    component_lock = canonical_identity({"fixture": "component-lock"})
    bound_value = dict(receipt_value)
    bound_value["project_revision"] = rebuild_project_authority_identity(
        ContentIdentity.parse_uri(str(receipt_value["project_revision"])),
        (component_lock,),
    ).uri
    receipt = ProjectTestReceipt.from_dict(bound_value)
    provisional = ProjectTestReceiptProvisional(
        lifecycle_request_identity=_FINALIZATION_EVIDENCE["lifecycle-request"],
        lifecycle_command_identity=_FINALIZATION_EVIDENCE["lifecycle-command"],
        source_cache_control_identity=canonical_identity({"fixture": "control"}),
        component_lock_identities=(component_lock,),
        receipt_identity=receipt.identity,
        receipt=receipt,
    )
    return ProjectTestReceiptFinalizedCandidate.finalize(
        provisional,
        source_cache_decision_identity=_FINALIZATION_EVIDENCE["source-cache-decision"],
        source_cache_lifecycle_identity=_FINALIZATION_EVIDENCE[
            "source-cache-lifecycle"
        ],
    ).to_dict()


class ProjectTestReceiptCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Initialize and configure the starter project once; every test works
        # on its own copy and never mutates the template.
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.template = Path(directory.name) / "demo"
        status, envelope = invoke(
            "init",
            str(cls.template),
            "--flavor",
            "python",
            "--flavor",
            "macos",
            "--flavor",
            "bazel",
        )
        if status != 0:
            raise AssertionError(envelope)
        configure_receipt_policy(cls.template)

    def copy_template(self, target: Path) -> None:
        shutil.copytree(self.template, target, symlinks=True)

    def test_update_canonicalizes_atomically_and_check_requires_exact_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "demo"
            self.copy_template(target)
            candidate = root / "candidate.json"
            candidate.write_text(
                json.dumps(finalize_candidate(make_candidate(target)), indent=2) + "\n",
                encoding="utf-8",
            )

            status, envelope = invoke(
                "project",
                "test-receipt",
                "update",
                str(candidate),
                "--project",
                str(target),
            )

            self.assertEqual(status, 0)
            self.assertTrue(envelope["result"]["updated"])
            committed = target / "verification" / "current.json"
            self.assertTrue(committed.is_file())
            self.assertNotIn("\n  ", committed.read_text(encoding="utf-8"))

            status, envelope = invoke(
                "project",
                "test-receipt",
                "check",
                str(candidate),
                "--project",
                str(target),
            )
            self.assertEqual(status, 0)
            self.assertTrue(envelope["result"]["matched"])

            status, envelope = invoke(
                "project",
                "test-receipt",
                "require-current",
                "--project",
                str(target),
            )
            self.assertEqual(status, 0)
            self.assertEqual(envelope["result"]["state"], "current")
            self.assertEqual(
                envelope["result"]["project_revision_identity"],
                envelope["result"]["current_project_revision_identity"],
            )

            status, envelope = invoke(
                "project",
                "test-receipt",
                "update",
                str(candidate),
                "--project",
                str(target),
            )
            self.assertEqual(status, 0)
            self.assertFalse(envelope["result"]["updated"])

    def test_failed_or_wrong_project_candidate_cannot_replace_current_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "demo"
            self.copy_template(target)
            candidate = root / "candidate.json"
            value = finalize_candidate(make_candidate(target))
            value["receipt"]["outcome"] = "failed"
            value["receipt"]["summary"] = {
                "total": 4,
                "passed": 3,
                "failed": 1,
                "skipped": 0,
            }
            candidate.write_text(json.dumps(value), encoding="utf-8")

            status, envelope = invoke(
                "project",
                "test-receipt",
                "update",
                str(candidate),
                "--project",
                str(target),
            )

            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"], "project.test_receipt_candidate_invalid"
            )
            self.assertFalse((target / "verification" / "current.json").exists())

            candidate.write_text(
                json.dumps(
                    finalize_candidate(
                        make_candidate(target, project_id="another-project")
                    )
                ),
                encoding="utf-8",
            )
            status, envelope = invoke(
                "project",
                "test-receipt",
                "update",
                str(candidate),
                "--project",
                str(target),
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"], "project.test_receipt_project_mismatch"
            )

    def test_component_and_specification_changes_stale_the_receipt(self):
        for mutation in ("component", "specification"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                target = root / "demo"
                self.copy_template(target)
                copy_generation_catalogs(target)
                component = copy_hello_component(target)
                refresh_authority_review(target)
                candidate = root / "candidate.json"
                candidate.write_text(
                    json.dumps(finalize_candidate(make_candidate(target))),
                    encoding="utf-8",
                )
                self.assertEqual(
                    invoke(
                        "project",
                        "test-receipt",
                        "update",
                        str(candidate),
                        "--project",
                        str(target),
                    )[0],
                    0,
                )

                if mutation == "component":
                    specification = component / "component.md"
                    specification.write_text(
                        specification.read_text(encoding="utf-8").replace(
                            "version: 1.0.0", "version: 1.0.1", 1
                        ),
                        encoding="utf-8",
                    )
                else:
                    specification = component / "component.md"
                    specification.write_text(
                        specification.read_text(encoding="utf-8")
                        + "\n<!-- receipt freshness regression -->\n",
                        encoding="utf-8",
                    )
                refresh_authority_review(target)

                status, envelope = invoke("project", "validate", str(target))
                self.assertEqual(status, 0)
                self.assertEqual(envelope["result"]["test_receipt"]["state"], "stale")
                status, envelope = invoke(
                    "project",
                    "test-receipt",
                    "require-current",
                    "--project",
                    str(target),
                )
                self.assertEqual(status, 2)
                self.assertEqual(
                    envelope["error"]["code"], "project.test_receipt_stale"
                )


if __name__ == "__main__":
    unittest.main()
