"""Fast parser, help, and canonical request checks for public refresh."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters import repository_refresh_planning as planning
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_planning import (
    load_repository_refresh_request,
)
from literate_ai.cli import dispatch
from literate_ai.cli.orchestration import human_orchestration_text
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)


class RepositoryRefreshCliSurfaceTests(unittest.TestCase):
    def test_parser_and_help_expose_reviewed_refresh_operations(self):
        parser = dispatch._parser()
        args = parser.parse_args(
            [
                "onboard",
                "orchestrate",
                "refresh",
                "apply",
                "/project",
                "--request",
                "/request.json",
                "--expected-plan-identity",
                "sha256:" + "1" * 64,
                "--acknowledge",
                "--repository-fetch-total-seconds",
                "120",
                "--repository-fetch-no-progress-seconds",
                "60",
                "--repository-fetch-connect-seconds",
                "10",
            ]
        )
        self.assertEqual(args.orchestration_command, "refresh")
        self.assertEqual(args.refresh_command, "apply")
        self.assertTrue(args.acknowledge)
        help_text = dispatch._help_topic_parser(
            parser, ("onboard", "orchestrate", "refresh", "apply")
        ).format_help()
        for option in (
            "--request",
            "--expected-plan-identity",
            "--acknowledge",
            "--repository-fetch-total-seconds",
        ):
            self.assertIn(option, help_text)

    def test_request_file_requires_exact_canonical_typed_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "request.json"
            request = RepositoryRefreshRequest(
                (RepositoryRefreshTarget("app", "1" * 40),)
            )
            canonical = canonical_json_bytes(request.to_dict()) + b"\n"
            path.write_bytes(canonical)
            self.assertEqual(load_repository_refresh_request(path), request)
            path.write_bytes(canonical + b"\n")
            with self.assertRaises(OrchestrationInventoryError) as caught:
                load_repository_refresh_request(path)
            self.assertEqual(
                caught.exception.code, "orchestration.refresh_request_invalid"
            )

    def test_noop_human_result_does_not_claim_a_manifest_write(self):
        text = human_orchestration_text(
            {
                "schema": "literate-ai/orchestration-refresh-apply@1",
                "state": "no-op",
                "changed": False,
                "authority_changed": False,
                "filesystem_writes": False,
                "source_writes": False,
                "writes": False,
                "cleanup_retained": [],
                "plan_identity": "sha256:" + "1" * 64,
            }
        )
        self.assertIn("Exact no-op", text)
        self.assertNotIn("Manifest committed", text)

    def test_committed_cleanup_retention_is_public_and_not_replay_authority(self):
        identity = "sha256:" + "1" * 64
        plan = {
            key: identity
            for key in (
                "request_identity",
                "previous_authority_identity",
                "prospective_authority_identity",
                "publication_identity",
                "publication_custody_identity",
                "deadline_policy_identity",
            )
        }
        plan.update(source_writes=False, target_modes=[])
        result = planning._refresh_apply_result(
            plan,
            identity,
            SimpleNamespace(
                state="committed",
                changed=True,
                source_writes=False,
                cleanup_retained=("terminal-refresh-staging",),
                authority_identity=identity,
            ),
        )
        self.assertEqual(result["state"], "committed-with-cleanup-retained")
        self.assertEqual(result["transaction_state"], "committed")
        self.assertTrue(result["authority_changed"])
        self.assertTrue(result["filesystem_writes"])
        self.assertEqual(result["cleanup_retained"], ["terminal-refresh-staging"])
        self.assertEqual(result["crash_replay"], "not-supported")
        text = human_orchestration_text(result)
        self.assertIn("Commit succeeded", text)
        self.assertIn("not replay authority", text)

    def test_retained_noop_reports_persistent_filesystem_writes(self):
        identity = "sha256:" + "2" * 64
        plan = {
            key: identity
            for key in (
                "request_identity",
                "previous_authority_identity",
                "prospective_authority_identity",
                "publication_identity",
                "publication_custody_identity",
                "deadline_policy_identity",
            )
        }
        plan.update(source_writes=False, target_modes=[])
        result = planning._refresh_apply_result(
            plan,
            identity,
            SimpleNamespace(
                state="committed",
                changed=False,
                source_writes=False,
                cleanup_retained=(
                    "terminal-refresh-staging",
                    "stage-marker-artifacts",
                ),
                authority_identity=identity,
            ),
        )
        self.assertFalse(result["authority_changed"])
        self.assertTrue(result["filesystem_writes"])
        self.assertTrue(result["writes"])
        self.assertFalse(result["authority_review_required"])
        text = human_orchestration_text(result)
        self.assertIn("Root authority is unchanged", text)
        self.assertIn("persistent filesystem writes remain", text)
        self.assertNotIn("no repository files", text)


if __name__ == "__main__":
    unittest.main()
