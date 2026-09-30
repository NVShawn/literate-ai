"""Public reviewed refresh CLI journeys over a reconstructed clean clone."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_planning as planning
from literate_ai.cli import dispatch, main
from literate_ai.cli.orchestration import human_orchestration_text
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from literate_ai.projects import parse_project_configuration
from tests.unit import test_refresh_file_custody as fixtures
from tests.unit.test_repository_orchestration import git, snapshot


class TtyStringIO(io.StringIO):
    def isatty(self):
        return True


class RepositoryRefreshCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        harness = fixtures.RefreshFileCustodyTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        source = harness.fixture
        self.target = harness.publish("source.txt", b"public refresh\n")
        git(source.root, "add", "-A")
        git(source.root, "commit", "-q", "-m", "complete root authority")
        source_root_remote = self.base / "super.git"
        source_app_remote = self.base / "app.git"
        source_lib_remote = self.base / "lib.git"
        git(
            self.base,
            "clone",
            "--bare",
            "--no-hardlinks",
            str(source.root),
            str(source_root_remote),
        )
        git(
            self.base,
            "clone",
            "--bare",
            "--no-hardlinks",
            str(source.remote),
            str(source_app_remote),
        )
        git(
            self.base,
            "clone",
            "--bare",
            "--no-hardlinks",
            str(source.root / "lib"),
            str(source_lib_remote),
        )
        self.root = self.base / "clean"
        git(self.base, "clone", "-q", source_root_remote.as_uri(), str(self.root))
        git(
            self.base,
            "clone",
            "-q",
            "--no-hardlinks",
            source_app_remote.as_uri(),
            str(self.root / "app"),
        )
        git(
            self.root / "app",
            "checkout",
            "-q",
            source.pin,
        )
        git(
            self.base,
            "clone",
            "-q",
            "--no-hardlinks",
            source_lib_remote.as_uri(),
            str(self.root / "lib"),
        )
        git(self.root / "lib", "checkout", "-q", source.pin)
        for child in (self.root, self.root / "app", self.root / "lib"):
            git(child, "config", "user.name", "Test")
            git(child, "config", "user.email", "test@example.test")
        self.assertEqual(
            git(
                self.root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
            b"",
        )
        self.request_path = self.base / "refresh.json"
        self.write_request(self.target)

    def write_request(self, commit):
        request = RepositoryRefreshRequest((RepositoryRefreshTarget("app", commit),))
        self.request_path.write_bytes(canonical_json_bytes(request.to_dict()) + b"\n")
        return request

    def invoke(self, operation, *options, json_mode=True):
        output = io.StringIO() if json_mode else TtyStringIO()
        errors = io.StringIO()
        arguments = [
            "onboard",
            "orchestrate",
            "refresh",
            operation,
            str(self.root),
            "--request",
            str(self.request_path),
            "--repository-fetch-total-seconds",
            "120",
            "--repository-fetch-no-progress-seconds",
            "60",
            "--repository-fetch-connect-seconds",
            "10",
            *options,
        ]
        if json_mode:
            arguments.append("--json")
        with ExitStack() as stack:
            for name in (
                "maybe_host_self_update",
                "ensure_user_mcp_catalog",
                "journal_mutagenic_event",
                "_fan_out_operator_mcp_event",
                "_run_operator_mcp_discovery",
            ):
                stack.enter_context(
                    patch.object(dispatch, name, side_effect=AssertionError(name))
                )
            stack.enter_context(
                patch.object(
                    dispatch.PerformanceRecorder,
                    "span",
                    side_effect=AssertionError("telemetry"),
                )
            )
            code = main(arguments, stdout=output, stderr=errors)
        text = output.getvalue() if code == 0 else errors.getvalue()
        return code, json.loads(text) if json_mode else text

    def test_public_plan_check_apply_and_noop_from_clean_clone(self):
        before = snapshot(self.root)
        code, envelope = self.invoke("plan")
        self.assertEqual(code, 0, envelope)
        plan = envelope["result"]
        self.assertEqual(plan["schema"], planning.PLAN_SCHEMA)
        self.assertTrue(plan["apply_supported"])
        self.assertFalse(plan["writes"])
        self.assertTrue(plan["source_writes"])
        self.assertEqual(
            plan["target_modes"], [{"path": "app", "mode": "source-transition"}]
        )
        self.assertEqual(snapshot(self.root), before)

        code, checked = self.invoke(
            "check", "--expected-plan-identity", plan["plan_identity"]
        )
        self.assertEqual(code, 0, checked)
        self.assertEqual(checked["result"]["state"], "current")
        code, missing = self.invoke(
            "apply", "--expected-plan-identity", plan["plan_identity"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(
            missing["error"]["code"],
            "orchestration.refresh_acknowledgement_required",
        )
        code, stale = self.invoke(
            "check", "--expected-plan-identity", "sha256:" + "0" * 64
        )
        self.assertEqual(code, 2)
        self.assertEqual(stale["error"]["code"], "orchestration.refresh_plan_stale")
        self.assertEqual(snapshot(self.root), before)

        code, envelope = self.invoke(
            "apply",
            "--expected-plan-identity",
            plan["plan_identity"],
            "--acknowledge",
        )
        self.assertEqual(code, 0, envelope)
        result = envelope["result"]
        self.assertEqual(result["schema"], planning.APPLY_SCHEMA)
        self.assertEqual(result["state"], "committed")
        self.assertEqual(result["transaction_state"], "committed")
        self.assertTrue(result["apply_supported"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["writes"])
        self.assertTrue(result["source_writes"])
        self.assertEqual(result["target_modes"], plan["target_modes"])
        self.assertTrue(result["authority_review_required"])
        self.assertEqual(result["child_acceptance"], "not-qualified")
        self.assertEqual(result["crash_replay"], "not-supported")
        self.assertEqual(
            result["result_identity"],
            canonical_identity(
                {
                    key: value
                    for key, value in result.items()
                    if key != "result_identity"
                }
            ).uri,
        )
        self.assertEqual(
            git(self.root / "app", "rev-parse", "HEAD").decode().strip(),
            self.target,
        )
        self.assertEqual(
            git(self.root, "ls-files", "--stage", "app").decode().split()[1],
            self.target,
        )
        definition = parse_project_configuration(
            (self.root / "literate.project.json").read_bytes()
        )
        pin = next(
            item
            for item in definition.repository_orchestration.repositories
            if item.path == "app"
        )
        self.assertEqual(pin.commit, self.target)

    def test_public_exact_head_refresh_reports_root_pin_only_without_child_writes(self):
        git(self.root / "app", "checkout", "-q", self.target)
        child_before = snapshot(self.root / "app")
        code, envelope = self.invoke("plan")
        self.assertEqual(code, 0, envelope)
        plan = envelope["result"]
        self.assertEqual(
            plan["target_modes"], [{"path": "app", "mode": "root-pin-only"}]
        )
        self.assertFalse(plan["source_writes"])
        code, envelope = self.invoke(
            "apply",
            "--expected-plan-identity",
            plan["plan_identity"],
            "--acknowledge",
        )
        self.assertEqual(code, 0, envelope)
        result = envelope["result"]
        self.assertFalse(result["source_writes"])
        self.assertEqual(result["target_modes"], plan["target_modes"])
        self.assertEqual(snapshot(self.root / "app"), child_before)
        self.assertEqual(
            git(self.root, "ls-files", "--stage", "app").decode().split()[1],
            self.target,
        )

    def test_public_exact_noop_reports_no_writes_and_preserves_clone(self):
        current = git(self.root / "app", "rev-parse", "HEAD").decode().strip()
        self.write_request(current)
        before_noop = snapshot(self.root)
        code, envelope = self.invoke("plan")
        self.assertEqual(code, 0, envelope)
        noop_plan = envelope["result"]
        self.assertFalse(noop_plan["changed"])
        code, envelope = self.invoke(
            "apply",
            "--expected-plan-identity",
            noop_plan["plan_identity"],
            "--acknowledge",
        )
        self.assertEqual(code, 0, envelope)
        noop = envelope["result"]
        self.assertEqual(noop["state"], "no-op")
        self.assertFalse(noop["changed"])
        self.assertFalse(noop["writes"])
        self.assertFalse(noop["authority_review_required"])
        self.assertEqual(snapshot(self.root), before_noop)
        self.assertIn(
            "Child acceptance and crash replay are not qualified",
            human_orchestration_text(noop),
        )

    def test_human_plan_output_names_review_boundary(self):
        code, text = self.invoke("plan", json_mode=False)
        self.assertEqual(code, 0, text)
        self.assertIn("orchestration refresh: reviewed plan", text)
        self.assertIn("Child acceptance and crash replay are not qualified", text)


if __name__ == "__main__":
    unittest.main()
