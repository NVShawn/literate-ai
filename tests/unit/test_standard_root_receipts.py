"""Public root rebuild publication binds the whole accepted Component set."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.application.standard_test_receipts import (
    StandardTestReceiptProjectionError,
    combine_standard_project_test_receipts,
)
from literate_ai.contracts import (
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
    load_current_standard_lifecycle_policy,
    rebuild_project_authority_identity,
)
from tests.support import fixtures_test_cli_rebuild as cli_fixture

POLICY = load_current_standard_lifecycle_policy()
BASE = canonical_identity("project-authority")
RUNNER = canonical_identity("standard-runner")
RECEIPT_POLICY = ProjectTestReceiptPolicy(
    POLICY.policy_id,
    POLICY.policy_version,
    RUNNER,
    tuple(sorted(POLICY.required_evidence_kinds)),
    1,
)
LABELS = ("components/alpha", "components/beta")
LOCKS = tuple(
    sorted((canonical_identity(label) for label in LABELS), key=lambda i: i.uri)
)


def candidate(label, *, base=BASE, runner=RUNNER):
    evidence = {
        kind: canonical_identity((label, kind))
        for kind in POLICY.required_evidence_kinds
    }
    evidence["test-runner"] = runner
    receipt = ProjectTestReceipt(
        project_id="fixture",
        project_revision_identity=rebuild_project_authority_identity(
            base, (canonical_identity(label),)
        ),
        subject_identity=canonical_identity((label, "subject")),
        suite=VersionedContentRef(
            "test-suite", POLICY.policy_id, POLICY.policy_version, POLICY.identity
        ),
        outcome="passed",
        summary=ProjectTestSummary(3, 3, 0, 0),
        result_identity=canonical_identity((label, "result")),
        evidence=tuple(ProjectTestEvidence(k, v) for k, v in sorted(evidence.items())),
    )
    return ProjectTestReceiptFinalizedCandidate.finalize(
        ProjectTestReceiptProvisional(
            evidence["lifecycle-request"],
            evidence["lifecycle-command"],
            canonical_identity((label, "cache-control")),
            (canonical_identity(label),),
            receipt.identity,
            receipt,
        ),
        source_cache_decision_identity=evidence["source-cache-decision"],
        source_cache_lifecycle_identity=evidence["source-cache-lifecycle"],
    )


def combine(members, *, locks=LOCKS):
    return combine_standard_project_test_receipts(
        tuple(members),
        project_id="fixture",
        validated_project_identity=BASE,
        component_lock_identities=locks,
        lifecycle_policy=POLICY,
        receipt_policy=RECEIPT_POLICY,
    )


class StandardRootReceiptTests(unittest.TestCase):
    def exercise(self, *, change=None, publish=True, count=2):
        """Accepted execution is injected; CLI custody and publication are real."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = cli_fixture.RebuildCliTests()
            project = fixture._project(root)
            fixture._configure_standard_driver(project)
            fixture._clear_component_locks(project)
            for label in reversed(LABELS[:count]):
                fixture._add_locked_component(project, label)
            manifest_path = project / "literate.project.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["test_receipt_policy"] = RECEIPT_POLICY.to_dict()
            manifest_path.write_text(json.dumps(manifest))
            binding = Mock(policy=POLICY)
            calls = []
            snapshots = []

            def execute(_project, _binding, args, component, label, **kwargs):
                calls.append(
                    (label, kwargs["update_receipt"], kwargs["write_candidate"])
                )
                snapshot = Mock()
                snapshots.append(snapshot)
                if change == "snapshot" and label == LABELS[0]:
                    snapshot.require_unchanged.side_effect = (
                        LockedGenerationAuthorityReaderError(
                            "fixture.source_changed", "earlier Component changed"
                        )
                    )
                if change == "new-lock" and label == LABELS[-1]:
                    fixture._add_locked_component(project, "components/gamma")
                if change == "missing-member" and label == LABELS[0]:
                    member = candidate(LABELS[-1])
                else:
                    member = candidate(label)
                if kwargs["observer"] is not None:
                    kwargs["observer"](
                        SimpleNamespace(finalized_receipt=member),
                        None,
                        SimpleNamespace(locked_authority_snapshot=snapshot),
                        None,
                    )
                result = fixture._component_rebuild_result(label, update_receipt=False)
                result["component_lock_identity"] = canonical_identity(label).uri
                result["component_lock_identities"] = [canonical_identity(label).uri]
                return result

            target = root / "candidate.json"
            with (
                patch(
                    "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                    return_value=binding,
                ),
                patch(
                    "literate_ai.cli.rebuild._standard_rebuild_one_component",
                    side_effect=execute,
                ),
                patch(
                    "literate_ai.cli.rebuild.validated_project_authority_identity",
                    side_effect=[BASE, canonical_identity("changed")]
                    if change == "project"
                    else None,
                    return_value=BASE,
                ),
                patch(
                    "literate_ai.cli.rebuild.current_project_component_lock_identities",
                    side_effect=[LOCKS, (canonical_identity("changed"),)]
                    if change == "locks"
                    else None,
                    return_value=LOCKS,
                ),
            ):
                status, envelope = cli_fixture.invoke(
                    "rebuild",
                    ".",
                    "--project",
                    str(project),
                    "--allow-host-execution",
                    *(
                        ("--update-receipt", "--candidate-receipt", str(target))
                        if publish
                        else ()
                    ),
                )
            tracked = project / manifest["test_receipt"]
            if change:
                self.assertNotEqual(status, 0, envelope)
                self.assertEqual(
                    envelope["error"]["code"],
                    {
                        "snapshot": "fixture.source_changed",
                        "missing-member": "standard_receipt.root_set_mismatch",
                    }.get(change, "rebuild.standard_lock_set_changed"),
                )
                self.assertFalse(target.exists())
                self.assertFalse(tracked.exists())
            else:
                self.assertEqual(status, 0, envelope)
                if count > 1:
                    self.assertEqual(calls, [(label, False, False) for label in LABELS])
                    for snapshot in snapshots:
                        snapshot.require_unchanged.assert_called_once()
                    expected = combine(candidate(label) for label in LABELS)
                    result = envelope["result"]
                    self.assertEqual(
                        result["component_lock_identities"], [i.uri for i in LOCKS]
                    )
                    self.assertEqual(
                        result["test_summary"], expected.receipt.summary.to_dict()
                    )
                    self.assertEqual(
                        result["receipt_identity"], expected.receipt_identity.uri
                    )
                    self.assertEqual(
                        result["finalized_candidate_identity"], expected.identity.uri
                    )
                    self.assertEqual(
                        result["project_revision_identity"],
                        expected.receipt.project_revision_identity.uri,
                    )
                    self.assertEqual(
                        [i["specification"] for i in result["components"]], list(LABELS)
                    )
                    self.assertNotIn("artifact", result)
                    if publish:
                        self.assertEqual(
                            json.loads(target.read_text()), expected.to_dict()
                        )
                        self.assertEqual(tracked.read_bytes(), target.read_bytes())
                        self.assertTrue(result["receipt_committed"])
                    else:
                        self.assertFalse(result["receipt_committed"])
                        self.assertFalse(tracked.exists())
                else:
                    self.assertEqual(calls, [(LABELS[0], False, True)])
            return envelope

    def test_public_candidate_and_committed_receipt_bind_both_components(self):
        self.exercise()

    def test_public_result_without_publication_still_binds_both_components(self):
        self.exercise(publish=False)

    def test_project_lock_set_and_earlier_snapshot_changes_refuse_publication(self):
        for change in ("project", "locks", "snapshot", "new-lock", "missing-member"):
            with self.subTest(change=change):
                self.exercise(change=change)

    def test_one_root_keeps_the_single_component_path(self):
        self.exercise(publish=False, count=1)

    def test_combination_preserves_one_root_and_binds_every_member(self):
        first, second = (candidate(label) for label in LABELS)
        self.assertIs(combine((first,), locks=first.component_lock_identities), first)
        self.assertEqual(combine((first, second)), combine((second, first)))
        changed = replace(
            first.receipt, result_identity=canonical_identity("other-result")
        )
        provisional = ProjectTestReceiptProvisional(
            first.lifecycle_request_identity,
            first.lifecycle_command_identity,
            first.source_cache_control_identity,
            first.component_lock_identities,
            changed.identity,
            changed,
        )
        changed_first = ProjectTestReceiptFinalizedCandidate.finalize(
            provisional,
            source_cache_decision_identity=first.source_cache_decision_identity,
            source_cache_lifecycle_identity=first.source_cache_lifecycle_identity,
        )
        self.assertNotEqual(
            combine((first, second)).identity, combine((changed_first, second)).identity
        )

    def test_incomplete_foreign_or_duplicate_receipts_cannot_form_root_proof(self):
        first, second = (candidate(label) for label in LABELS)
        for members in (
            (),
            (first,),
            (first, first),
            (candidate(LABELS[0], base=canonical_identity("old")), second),
            (candidate(LABELS[0], runner=canonical_identity("foreign")), second),
        ):
            with self.subTest(members=tuple(i.identity.uri for i in members)):
                with self.assertRaises(StandardTestReceiptProjectionError):
                    combine(members)
