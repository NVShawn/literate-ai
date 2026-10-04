"""CLI tests for the replaceable, Git-history-friendly project test receipt."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli import main
from literate_ai.contracts import (
    PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA,
    ContentIdentity,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
    canonical_json_bytes,
    rebuild_project_authority_identity,
)
from literate_ai.projects import load_project
from literate_ai.test_receipts import (
    update_project_test_receipt_finalized_value,
    update_project_test_receipt_value,
)
from tests.unit.root_parent_adapter import root_parent_for_fixture_project
from tests.support.fixtures_test_project_cli import (
    copy_generation_catalogs,
    copy_hello_component,
    refresh_authority_review,
)

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
    def test_current_receipt_accepts_exact_project_and_component_lock_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
            receipt_value = make_candidate(target)
            ephemeral_lock = canonical_identity({"fixture": "component-lock"})
            candidate = Path(directory) / "candidate.json"
            candidate.write_bytes(
                canonical_json_bytes(finalize_candidate(receipt_value)) + b"\n"
            )

            status, updated = invoke(
                "project",
                "test-receipt",
                "update",
                str(candidate),
                "--project",
                str(target),
            )
            self.assertEqual(status, 0, updated)
            tracked = json.loads(
                (target / "verification" / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                tracked["schema"], PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA
            )
            self.assertEqual(tracked["component_lock_identities"], [ephemeral_lock.uri])
            status, current = invoke(
                "project",
                "test-receipt",
                "require-current",
                "--project",
                str(target),
            )
            self.assertEqual(status, 0, current)
            self.assertEqual(current["result"]["state"], "current")
            self.assertEqual(current["result"]["component_lock_count"], 1)

    def test_in_memory_candidate_uses_the_same_atomic_finalization_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
            receipt = ProjectTestReceipt.from_dict(make_candidate(target))
            project = load_project(target)

            result = update_project_test_receipt_value(
                project,
                receipt,
                project_revision_identity=receipt.project_revision_identity,
            )

            self.assertTrue(result["updated"])
            committed = ProjectTestReceipt.from_dict(
                json.loads(
                    (target / "verification" / "current.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            self.assertEqual(committed, receipt)

    def test_in_memory_finalized_candidate_retains_exact_lock_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
            base_value = make_candidate(target)
            finalized = ProjectTestReceiptFinalizedCandidate.from_dict(
                finalize_candidate(base_value)
            )
            project = load_project(target)
            base_revision = ContentIdentity.parse_uri(
                str(base_value["project_revision"])
            )

            result = update_project_test_receipt_finalized_value(
                project,
                finalized,
                project_revision_identity=base_revision,
            )

            self.assertTrue(result["updated"])
            tracked = json.loads(
                (target / "verification" / "current.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                tracked["schema"], PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA
            )
            status, current = invoke(
                "project",
                "test-receipt",
                "require-current",
                "--project",
                str(target),
            )
            self.assertEqual(status, 0, current)
            self.assertEqual(current["result"]["state"], "current")

    def test_deeply_nested_candidate_fails_closed_without_parser_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
            candidate = root / "deep.json"
            candidate.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")

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
                envelope["error"]["code"],
                "project.test_receipt_candidate_invalid",
            )

    def test_missing_receipt_is_visible_but_does_not_invalidate_new_project(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 0)
            self.assertEqual(
                envelope["result"]["test_receipt"],
                {
                    "configured": True,
                    "path": "verification/current.json",
                    "policy_configured": False,
                    "state": "policy-unconfigured",
                },
            )
            status, envelope = invoke(
                "project",
                "test-receipt",
                "require-current",
                "--project",
                str(target),
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"],
                "project.test_receipt_policy_unconfigured",
            )

            candidate = Path(directory) / "candidate.json"
            candidate.write_text(
                json.dumps(
                    finalize_candidate(make_candidate(target, policy=fixture_policy()))
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
                envelope["error"]["code"],
                "project.test_receipt_policy_unconfigured",
            )
            self.assertFalse((target / "verification" / "current.json").exists())

    def test_update_canonicalizes_atomically_and_check_requires_exact_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
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
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
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

    def test_policy_rejects_arbitrary_suite_evidence_runner_and_test_count(self):
        mutations = {
            "suite-id": lambda value: value["suite"].__setitem__(
                "id", "arbitrary-suite"
            ),
            "suite-version": lambda value: value["suite"].__setitem__(
                "version", "9.9.9"
            ),
            "missing-policy-evidence": lambda value: value.__setitem__(
                "evidence",
                {kind: value["evidence"][kind] for kind in _FINALIZATION_EVIDENCE},
            ),
            "wrong-runner": lambda value: value["evidence"].__setitem__(
                "test-runner", canonical_identity({"fixture": "impostor"}).uri
            ),
            "too-few-tests": lambda value: value.__setitem__("tests", 3),
        }
        for label, mutate in mutations.items():
            with (
                self.subTest(mutation=label),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                target = root / "demo"
                self.assertEqual(
                    invoke(
                        "init",
                        str(target),
                        "--flavor",
                        "python",
                        "--flavor",
                        "macos",
                        "--flavor",
                        "bazel",
                    )[0],
                    0,
                )
                configure_receipt_policy(target)
                value = make_candidate(target)
                mutate(value)
                candidate = root / "candidate.json"
                candidate.write_text(
                    json.dumps(finalize_candidate(value)), encoding="utf-8"
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
                    envelope["error"]["code"],
                    "project.test_receipt_policy_mismatch",
                )
                self.assertFalse((target / "verification" / "current.json").exists())

    def test_current_gate_rejects_a_canonical_but_unauthorized_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
            value = make_candidate(target)
            value["evidence"]["test-runner"] = canonical_identity(
                {"fixture": "impostor"}
            ).uri
            receipt = ProjectTestReceipt.from_dict(value)
            committed = target / "verification" / "current.json"
            committed.parent.mkdir(exist_ok=True)
            committed.write_bytes(canonical_json_bytes(receipt.to_dict()) + b"\n")

            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 0)
            self.assertEqual(
                envelope["result"]["test_receipt"]["state"], "policy-mismatch"
            )
            status, envelope = invoke(
                "project",
                "test-receipt",
                "require-current",
                "--project",
                str(target),
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"], "project.test_receipt_policy_mismatch"
            )

    def test_component_and_specification_changes_stale_the_receipt(self):
        for mutation in ("component", "specification"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                target = root / "demo"
                self.assertEqual(
                    invoke(
                        "init",
                        str(target),
                        "--flavor",
                        "python",
                        "--flavor",
                        "macos",
                        "--flavor",
                        "bazel",
                    )[0],
                    0,
                )
                configure_receipt_policy(target)
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

    def test_manifest_change_makes_the_committed_receipt_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "demo"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            configure_receipt_policy(target)
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
            manifest = target / "literate.project.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["version"] = "1.0.1"
            manifest.write_text(json.dumps(value), encoding="utf-8")
            refresh_authority_review(target)

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 0)
            self.assertEqual(envelope["result"]["test_receipt"]["state"], "stale")
            status, envelope = invoke(
                "project",
                "test-receipt",
                "check",
                str(candidate),
                "--project",
                str(target),
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"], "project.test_receipt_project_mismatch"
            )


if __name__ == "__main__":
    unittest.main()
